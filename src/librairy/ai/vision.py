"""Ask a local model to look at one image and say what is in it.

This is deliberately not part of the `Provider` protocol. Classification asks
"what is this file?" and gets a category and a name back; this asks "what is in
this picture?" and gets a description, some subjects and any text it can read.
Bolting the second onto the first would mean every provider — including three
cloud ones that never see an image — growing a method it cannot answer.

**Nothing here ever runs against a cloud provider.** `describe_image` refuses
anything whose `ProviderConfig.is_local` is false, so image bytes cannot leave
your network by any configuration, opt-in or otherwise. That is a stronger
guarantee than the redaction the text path relies on, and it is stronger on
purpose: a redacted filename is a few words, and a photograph of your kitchen
is a photograph of your kitchen.

The model is only ever a source of evidence. It does not choose a category, a
folder or a filename — see `librairy/classify/images.py` for what LibrAIry does
with what it says.
"""

from __future__ import annotations

import base64
import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib import request
from urllib.error import HTTPError

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from librairy.ai.base import ProviderConfig
from librairy.ai.lmstudio import normalize_host

LOGGER = logging.getLogger(__name__)

# What ffmpeg can reliably decode from a still image without a second library.
# HEIC is deliberately absent: most ffmpeg builds cannot open it, and adding an
# image stack to a project with six runtime dependencies to caption a photo is
# not a trade worth making. An unsupported file is skipped, never failed.
VISION_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"})

# Longest edge sent to the model. A 48-megapixel phone photo carries no more
# semantic information than a 1280px copy of it, costs far more to encode, and
# on a small model actively hurts — the image is tiled and the tiles arrive as
# hundreds of tokens. 1280 is enough to read a screenshot's body text.
DEFAULT_MAX_EDGE = 1280
SCALE_TIMEOUT_SECONDS = 30
ERROR_SNIPPET_CHARS = 300

# Caps on what comes back. A model that decides to return its entire reasoning
# in `caption`, or four hundred tags, should be trimmed rather than trusted.
MAX_CAPTION = 300
MAX_TERMS = 12
MAX_TERM_CHARS = 40
MAX_TOKENS = 6
MAX_VISIBLE_TEXT = 4000

# The categories the model may answer with. These are *image* kinds, not
# LibrAIry categories — mapping one to the other is LibrAIry's job, and keeping
# the vocabularies separate is what stops a model from filing anything.
IMAGE_CATEGORIES = (
    "photo",
    "screenshot",
    "document",
    "receipt",
    "scan",
    "artwork",
    "diagram",
    "meme",
    "other",
)

VISION_PROMPT = """You are looking at one image so a private file organiser can
describe it. Reply with a single JSON object and nothing else.

Reply in exactly this shape:
{"category": "photo",
 "caption": "A baby sitting on a couch holding an orange cat.",
 "subjects": ["baby", "cat"],
 "tags": ["family", "indoor", "pet"],
 "visible_text": null,
 "filename_tokens": ["baby", "orange-cat"],
 "confidence": 0.89}

Rules:
- category is one of: photo, screenshot, document, receipt, scan, artwork,
  diagram, meme, other.
- caption is one plain sentence saying what is in the picture.
- subjects are the things actually in it: baby, child, person, two people,
  cat, dog, food, car, building, landscape.
- tags are short words for finding it again later.
- visible_text is the text you can read in the image, copied out as it appears,
  or null if there is none. Never put it in the caption instead.
- filename_tokens are two or three short words for naming the file. Lower case,
  no spaces, no punctuation, no file extension, no folders.
- confidence is a number between 0 and 1. Be honest — use a low value when
  guessing.

Never name or identify a person. "a man", "two people", "a child" is right; a
name is not. Do not guess anyone's ethnicity, health, religion, politics or
relationships, and do not guess an age beyond baby, child or adult. Describe
only what is visibly there."""


class VisionResult(BaseModel):
    """What the model says it saw. Every field is optional on purpose.

    A small model that answers with a caption and nothing else has still told
    us something useful, and throwing that away because `subjects` was missing
    would make the feature useless on exactly the models it is meant for.
    """

    model_config = ConfigDict(extra="ignore")

    category: str | None = None
    caption: str | None = None
    subjects: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    visible_text: str | None = None
    filename_tokens: tuple[str, ...] = ()
    confidence: float | None = None

    @field_validator("category", mode="before")
    @classmethod
    def known_category(cls, value: object) -> str | None:
        text = str(value or "").strip().lower()
        return text if text in IMAGE_CATEGORIES else None

    @field_validator("caption", mode="before")
    @classmethod
    def one_line(cls, value: object) -> str | None:
        text = " ".join(str(value or "").split())
        return text[:MAX_CAPTION] or None

    @field_validator("visible_text", mode="before")
    @classmethod
    def readable_text(cls, value: object) -> str | None:
        if value is None:
            return None
        # Line structure is most of what makes OCR out of a form or a receipt
        # legible, so unlike the caption this keeps its newlines.
        lines = [line.rstrip() for line in str(value).splitlines()]
        text = "\n".join(lines).strip()
        return text[:MAX_VISIBLE_TEXT] or None

    @field_validator("subjects", "tags", mode="before")
    @classmethod
    def term_list(cls, value: object) -> tuple[str, ...]:
        return _terms(value, MAX_TERMS)

    @field_validator("filename_tokens", mode="before")
    @classmethod
    def token_list(cls, value: object) -> tuple[str, ...]:
        # A token is a word for a filename, so anything that could make it a
        # path is dropped here rather than relied on being caught downstream.
        return tuple(
            token
            for token in _terms(value, MAX_TOKENS)
            if "/" not in token and "\\" not in token and not token.startswith(".")
        )

    @field_validator("confidence", mode="before")
    @classmethod
    def clamped(cls, value: object) -> float | None:
        if value is None:
            return None
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return None

    @property
    def empty(self) -> bool:
        """Nothing worth recording. A reply of `{}` validates but says nothing."""
        return not (self.caption or self.subjects or self.tags or self.visible_text)


def _terms(value: object, limit: int) -> tuple[str, ...]:
    """A list of short words, however the model chose to express it."""
    if isinstance(value, str):
        # Models routinely answer "baby, cat" where a list was asked for.
        items: list[object] = list(value.split(","))
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        return ()
    seen: list[str] = []
    for item in items:
        text = " ".join(str(item).split()).strip().lower()[:MAX_TERM_CHARS]
        if text and text not in seen:
            seen.append(text)
    return tuple(seen[:limit])


def validate_vision_response(text: str) -> VisionResult | None:
    """Parse a model reply. None means it gave us nothing usable.

    Never raises. Every caller is on the analysis path, where a model having a
    bad day must cost one file its caption and nothing else.
    """
    from librairy.ai.prompt import extract_json

    payload = extract_json(text or "")
    if payload is None:
        return None
    try:
        result = VisionResult.model_validate(payload)
    except ValidationError as exc:
        LOGGER.debug("vision reply rejected: %s", exc.errors()[0]["type"])
        return None
    return None if result.empty else result


def describe_image(
    config: ProviderConfig,
    path: Path,
    *,
    timeout: int,
    model: str = "",
    max_edge: int = DEFAULT_MAX_EDGE,
    settings=None,
) -> VisionResult | None:
    """Ask one local provider to describe one image file.

    `model` overrides the provider's chat model, because the model that reads
    your filenames and the model that looks at your photos have no reason to be
    the same one — and on a machine with limited memory they should not be.
    """
    if not config.is_local:
        # Not a configuration error to report; a rule. See the module docstring.
        LOGGER.debug("vision skipped: %s is not a local provider", config.name)
        return None
    if path.suffix.lower() not in VISION_EXTENSIONS:
        return None
    encoded = encoded_image(path, max_edge=max_edge, settings=settings)
    if encoded is None:
        return None
    reply = _ask(config, encoded, model or config.model, timeout)
    return validate_vision_response(reply) if reply else None


def _ask(config: ProviderConfig, encoded: str, model: str, timeout: int) -> str | None:
    if config.kind == "lmstudio":
        return _ask_openai_shaped(config, encoded, model, timeout)
    if config.kind == "ollama":
        return _ask_ollama(config, encoded, model, timeout)
    LOGGER.debug("vision: %s cannot be asked about images", config.kind)
    return None


def _ask_openai_shaped(
    config: ProviderConfig, encoded: str, model: str, timeout: int
) -> str | None:
    """LM Studio, and anything else speaking OpenAI's chat shape.

    No `response_format`: LM Studio's shim rejects `json_object` outright on
    current builds, exactly as it does for text classification. The prompt asks
    for one JSON object and the parser digs it out of whatever surrounds it.
    """
    body = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": VISION_PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
                    },
                ],
            }
        ],
    }
    url = f"{normalize_host(config.endpoint or '')}/v1/chat/completions"
    payload = _post(url, body, timeout, config.name)
    if payload is None:
        return None
    content = (payload.get("choices") or [{}])[0].get("message", {}).get("content")
    return content if isinstance(content, str) else None


def _ask_ollama(config: ProviderConfig, encoded: str, model: str, timeout: int) -> str | None:
    body = {
        "model": model,
        "prompt": VISION_PROMPT,
        "images": [encoded],
        "stream": False,
    }
    url = f"{(config.endpoint or '').rstrip('/')}/api/generate"
    payload = _post(url, body, timeout, config.name)
    if payload is None:
        return None
    response = payload.get("response")
    return response if isinstance(response, str) else None


def _post(url: str, body: dict, timeout: int, name: str) -> dict | None:
    data = json.dumps(body).encode()
    req = request.Request(  # noqa: S310 - operator-supplied LAN host
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer lm-studio"},
    )
    try:
        with request.urlopen(req, timeout=timeout) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        # A model that cannot see is a 400 here, and it is the single most
        # likely way this feature fails: the server is up, the model answers
        # text perfectly, and it has no vision tower. Say so out loud rather
        # than logging nothing and captioning nothing.
        LOGGER.warning("%s rejected the image (HTTP %s): %s", name, exc.code, _snippet(exc))
        return None
    except OSError as exc:
        LOGGER.warning("%s unreachable for image analysis: %s", name, exc)
        return None
    except json.JSONDecodeError:
        LOGGER.warning("%s returned a reply that was not JSON", name)
        return None


def _snippet(exc: HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", "replace")[:ERROR_SNIPPET_CHARS]
    except Exception:  # noqa: BLE001 - a failed error report is still an error
        return exc.reason or str(exc.code)


def encoded_image(path: Path, *, max_edge: int = DEFAULT_MAX_EDGE, settings=None) -> str | None:
    """A bounded-size JPEG copy of `path`, base64-encoded. None if that failed.

    The source file is never touched and the temporary copy never outlives the
    call. Scaling is ffmpeg's, which is already in the image for thumbnails and
    is the only image decoder this project ships.
    """
    if shutil.which("ffmpeg") is None:
        LOGGER.debug("vision skipped: no ffmpeg to resize %s", path.name)
        return None
    with tempfile.TemporaryDirectory(prefix="librairy-vision-") as workdir:
        target = Path(workdir) / "frame.jpg"
        if not _scale(path, target, max_edge, settings):
            return None
        return base64.b64encode(target.read_bytes()).decode("ascii")


def _scale(source: Path, target: Path, max_edge: int, settings) -> bool:
    filters = [FLATTEN_FILTER, *_orientation_filters(source, settings), _scale_filter(max_edge)]
    command = [
        "ffmpeg", "-y", "-loglevel", "error",
        # ffmpeg's own EXIF handling varies by build and by decoder, so the
        # rotation is applied explicitly below instead. Two builds silently
        # disagreeing about which way up a photo is would be a hard bug to
        # ever see, and a sideways photo reads as a different photo.
        "-noautorotate",
        "-i", str(source),
        "-vf", ",".join(filters),
        "-frames:v", "1", "-q:v", "3", "-f", "image2", str(target),
    ]
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            command, capture_output=True, timeout=SCALE_TIMEOUT_SECONDS, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        LOGGER.debug("vision resize failed for %s: %s", source, exc)
        return False
    if result.returncode != 0 or not target.exists() or target.stat().st_size == 0:
        LOGGER.debug("vision resize produced nothing for %s", source)
        return False
    return True


# Composite whatever is transparent onto white, always, before anything else.
#
# ffmpeg flattens an alpha channel onto **black**, and a great many PNGs have
# one — screenshots, diagrams, logos, anything exported from a design tool.
# Measured, not theorised: a screenshot of a Wi-Fi settings page reached the
# model as a solid black rectangle, and the model correctly described a solid
# black rectangle. The whole feature would have shipped captioning half of a
# photo library as "a featureless black image", with nothing anywhere saying
# why.
#
# `split` + `drawbox` makes an opaque white frame of exactly the input's size
# out of the input itself, so no dimensions have to be measured first and no
# deprecated filter is involved. Applied unconditionally: probing for an alpha
# channel would mean an ffprobe call to save work on a filter that costs
# nothing, and one code path is worth more than that.
FLATTEN_FILTER = (
    "split[bg][fg];"
    "[bg]drawbox=x=0:y=0:w=iw:h=ih:color=white@1:t=fill[white];"
    "[white][fg]overlay=format=auto"
)


def _scale_filter(max_edge: int) -> str:
    """Fit inside `max_edge` on the longest side, never upscaling.

    `min()` on both branches is what makes it never upscale; -2 keeps the other
    edge proportional and even, which the JPEG encoder requires.
    """
    edge = max(64, int(max_edge))
    return (
        f"scale=w='if(gt(iw,ih),min({edge},iw),-2)'"
        f":h='if(gt(iw,ih),-2,min({edge},ih))'"
    )


# EXIF orientation to the ffmpeg filters that undo it. Photographs come out of
# a phone stored sideways with a tag saying which way up they go, and a model
# shown a sideways photo describes a sideways photo.
ORIENTATION_FILTERS = {
    1: (),
    2: ("hflip",),
    3: ("transpose=1", "transpose=1"),
    4: ("vflip",),
    5: ("transpose=0",),
    6: ("transpose=1",),
    7: ("transpose=3",),
    8: ("transpose=2",),
}


def _orientation_filters(source: Path, settings) -> tuple[str, ...]:
    """How to turn this image the right way up, from its EXIF tag.

    exiftool is already shipped for photo metadata. No exiftool, no tag, or an
    unreadable one all mean "leave it alone" — being wrong about rotation is
    worse than not rotating.
    """
    if settings is None or shutil.which("exiftool") is None:
        return ()
    try:
        from librairy.tools.common import posix_path, run_json_tool

        result = run_json_tool(
            ["exiftool", "-j", "-n", "-Orientation", posix_path(source)], settings
        )
    except Exception as exc:  # noqa: BLE001 - metadata is best-effort
        LOGGER.debug("orientation lookup failed for %s: %s", source, exc)
        return ()
    if not result.ok or not isinstance(result.data, list) or not result.data:
        return ()
    try:
        orientation = int(result.data[0].get("Orientation", 1))
    except (TypeError, ValueError):
        return ()
    return ORIENTATION_FILTERS.get(orientation, ())
