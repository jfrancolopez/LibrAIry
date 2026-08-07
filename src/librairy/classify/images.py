"""What LibrAIry does with what a model saw in an image.

The split matters. `librairy/ai/vision.py` asks the question and validates the
answer; nothing there knows what a category or a destination is. Everything
that decides where a file goes lives here, in ordinary Python, and can be read
in one sitting:

* **The category never changes.** A model that says "receipt" about something
  the deterministic pass filed under photos is recorded as a disagreement and
  shown in Review, where the category dropdown is already one click away. It
  does not move the file. Re-filing on a caption is how a photo library
  quietly reorganises itself overnight.
* **The name is only ever added to.** Vision words are appended to a filename
  that says nothing — `IMG_4821.jpg`, a UUID, a bare number — and a name with
  a human word already in it is left exactly as it is. The words go through
  the same slugify every other name does.
* **Agreement raises confidence; disagreement never does.** One number, one
  rule, and a file the deterministic pass deliberately held below the
  threshold stays there.

Nothing here can produce a path. The model returns words; `render_destination`
builds the destination from the category and the fields, as it does for every
other kind of evidence.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, replace
from pathlib import PurePosixPath

from librairy.ai.base import ProviderConfig
from librairy.ai.registry import provider_chain
from librairy.ai.vision import VISION_EXTENSIONS, VisionResult, describe_image
from librairy.config import Settings
from librairy.models import EvidenceEntry, Item
from librairy.naming import EMBEDDED_UUID_RE, is_noise, slugify
from librairy.planner import utc_now

LOGGER = logging.getLogger(__name__)

# Ceiling on anything vision contributed to, matching the cap the text AI path
# already applies to itself. A local model looking at a JPEG is evidence, not
# certainty, and it should never be the reason a file reaches 1.00.
VISION_CONFIDENCE_CAP = 0.92
# What agreement is worth. Small on purpose: the deterministic pass already
# scored the file, and this only says a second, independent look agreed.
AGREEMENT_BONUS = 0.05
# Two failures against the same provider and the rest of the batch is skipped,
# the same rule the text providers use. A dead vision endpoint would otherwise
# cost one timeout per image, which on a 400-photo inbox is most of a day.
CIRCUIT_BREAK_FAILURES = 2

# The model answers with image kinds; LibrAIry has eight categories. Keeping
# the two vocabularies apart is what stops "receipt" from being a folder.
# `None` means "this tells us nothing about the category" rather than "misc".
CATEGORY_MAP: dict[str, str | None] = {
    "photo": "photos",
    "screenshot": "photos",
    "artwork": "photos",
    "meme": "photos",
    "document": "documents",
    "receipt": "documents",
    "scan": "documents",
    "diagram": "documents",
    "other": None,
}


@dataclass(frozen=True)
class StoredVision:
    """A vision answer read back out of the database."""

    provider: str
    model: str
    category: str | None
    caption: str | None
    subjects: tuple[str, ...]
    tags: tuple[str, ...]
    name_tokens: tuple[str, ...]
    visible_text: str | None
    confidence: float | None
    created_at: str

    @property
    def searchable(self) -> str:
        """Everything worth putting in the search index, as one blob."""
        parts = [self.caption or "", " ".join(self.subjects), " ".join(self.tags)]
        parts.append(self.visible_text or "")
        return " ".join(part for part in parts if part.strip())


def vision_wanted(settings: Settings, relpath: str, confidence: float) -> bool:
    """Whether this file should be looked at, before any work is done."""
    if not settings.vision_enabled:
        return False
    if PurePosixPath(relpath).suffix.lower() not in VISION_EXTENSIONS:
        return False
    if settings.vision_mode == "uncertain":
        return confidence < settings.confidence_threshold
    return True


def enrich_with_vision(
    conn: sqlite3.Connection,
    settings: Settings,
    item: Item,
    result,
    state=None,
    *,
    provider: ProviderConfig | None = None,
):
    """Look at the image and fold what came back into `result`.

    Returns `result` unchanged on every failure there is: feature off, not an
    image, no local provider, provider down, invalid JSON, empty answer. Image
    understanding is an enrichment, and an enrichment that can break analysis
    is not one.
    """
    if not vision_wanted(settings, item.relpath, result.confidence):
        return result
    stored = stored_vision(conn, item.id, fingerprint=item.fingerprint)
    if stored is not None:
        return apply_vision(settings, item, result, _as_answer(stored), stored.model)
    config = provider or local_vision_provider(conn, settings)
    if config is None:
        LOGGER.debug("vision skipped: no local AI provider is switched on")
        return result
    key = f"vision:{config.name}"
    if state is not None and state.failures.get(key, 0) >= CIRCUIT_BREAK_FAILURES:
        return result
    model = settings.vision_model.strip() or config.model
    path = settings.inbox_dir / item.relpath
    try:
        answer = describe_image(
            config,
            path,
            timeout=settings.ai_timeout,
            model=model,
            max_edge=settings.vision_max_edge,
            settings=settings,
        )
    except Exception as exc:  # noqa: BLE001 - never fail analysis over a caption
        LOGGER.warning("vision failed for %s: %s", item.relpath, exc)
        answer = None
    if answer is None:
        if state is not None:
            state.failures[key] = state.failures.get(key, 0) + 1
        return result
    save_vision(conn, item, answer, provider=config.name, model=model)
    return apply_vision(settings, item, result, answer, model)


def local_vision_provider(
    conn: sqlite3.Connection, settings: Settings
) -> ProviderConfig | None:
    """The first switched-on provider that runs on your own hardware.

    Cloud providers are not candidates and cannot be made into candidates —
    `describe_image` refuses them outright. This just means the picker never
    offers one, so a setup with only cloud AI configured reports "no local
    provider" instead of silently doing nothing.
    """
    for config in provider_chain(conn, settings, record=False):
        if config.is_local and config.kind in {"lmstudio", "ollama"}:
            return config
    return None


def apply_vision(settings: Settings, item: Item, result, answer: VisionResult, model: str):
    """Fold one vision answer into a classification result."""
    fields = dict(result.fields)
    clean_name = _named(result.clean_name, answer)
    if clean_name != result.clean_name:
        fields["clean_name"] = clean_name
    mapped = CATEGORY_MAP.get(answer.category or "")
    agrees = mapped is not None and mapped == result.category
    confidence = result.confidence
    if agrees:
        confidence = min(result.confidence + AGREEMENT_BONUS, VISION_CONFIDENCE_CAP)
    evidence = (*result.evidence, _evidence(answer, model, mapped, result.category))
    return replace(
        result,
        clean_name=clean_name,
        fields=fields,
        confidence=confidence,
        evidence=evidence,
    )


def _evidence(
    answer: VisionResult, model: str, mapped: str | None, category: str
) -> EvidenceEntry:
    detail = answer.caption or ", ".join(answer.subjects) or "looked at the image"
    if mapped is not None and mapped != category:
        detail = f"{detail} — looks more like {answer.category} than {category}"
    weight = answer.confidence if answer.confidence is not None else 0.5
    return EvidenceEntry("vision", "category", f"{model}: {detail}", min(weight, 1.0))


# Words that a phone, a camera or a screenshot tool puts in a filename when it
# has nothing to say about the contents.
DEVICE_WORDS = frozenset({
    "img", "image", "images", "dsc", "dscn", "dscf", "pic", "pict", "picture",
    "gopr", "dji", "mvimg", "pxl", "vid", "photo", "screenshot", "screen",
    "shot", "scr", "snap", "snapshot", "vlcsnap", "capture", "untitled",
    "unnamed", "download", "downloaded", "copy", "new", "file", "at", "am", "pm",
})
_HEX_BLOB = re.compile(r"(?i)^[0-9a-f]{8,}$")
_WORDS = re.compile(r"[-_. ]+")
# Enough to say what the picture is; more turns a filename into a sentence.
MAX_NAME_TOKENS = 3


def says_nothing(stem: str) -> bool:
    """Whether a filename stem carries a single word a person chose.

    `IMG_4821`, `PXL-20240612-101112`, a UUID out of iMessage and `00093` all
    say nothing. `Wedding-Day`, `budget-2026` and `IMG-holiday` all say
    something, and something is never overwritten.
    """
    # A UUID is taken out whole first. Split on separators it becomes seven
    # chunks, three of which are four hex characters — too short to tell from
    # a word, so "123F" read as meaningful and the stem looked informative.
    text = EMBEDDED_UUID_RE.sub(" ", stem).strip()
    if not text:
        return True
    for word in _WORDS.split(text):
        if not word or word.isdigit() or _HEX_BLOB.match(word):
            continue
        if word.lower() in DEVICE_WORDS:
            continue
        return False
    return True


def _named(clean_name: str, answer: VisionResult) -> str:
    """`clean_name` with the model's words appended, if it needs them.

    Appended rather than substituted: `IMG-20240612-101112.jpg` has already
    been rebuilt around its capture time by `photo_name`, and the date is what
    makes a photo folder sort. Adding to it costs nothing and losing it costs
    the ordering of the whole folder.
    """
    tokens = [slugify(token) for token in answer.filename_tokens[:MAX_NAME_TOKENS]]
    tokens = [token for token in tokens if token and token != "untitled"]
    if not tokens:
        return clean_name
    path = PurePosixPath(clean_name)
    suffix = path.suffix if len(path.suffix) <= 10 else ""
    stem = clean_name[: -len(suffix)] if suffix else clean_name
    if not says_nothing(stem):
        return clean_name
    words = "-".join(tokens)
    if words.lower() in stem.lower():
        return clean_name
    # A stem that is nothing but a UUID or a bare number is replaced outright
    # rather than kept as a prefix: 36 characters of hex in front of
    # "woman-portrait" is worse than either half alone, and a UUID is not a
    # disambiguator anybody can use. `IMG_4821` is kept, because the camera's
    # sequence number is how you find that photo again on the phone.
    if is_noise(stem):
        stem = ""
    stem = f"{stem}-{words}" if stem else words
    return f"{slugify(stem)}{suffix}"


def save_vision(
    conn: sqlite3.Connection, item: Item, answer: VisionResult, *, provider: str, model: str
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO vision_results(
          item_id, fingerprint, provider, model, category, caption,
          subjects, tags, name_tokens, visible_text, confidence, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            item.id,
            item.fingerprint or "",
            provider,
            model,
            answer.category,
            answer.caption,
            json.dumps(list(answer.subjects)),
            json.dumps(list(answer.tags)),
            json.dumps(list(answer.filename_tokens)),
            answer.visible_text,
            answer.confidence,
            utc_now(),
        ),
    )


def stored_vision(
    conn: sqlite3.Connection, item_id: int, *, fingerprint: str | None = None
) -> StoredVision | None:
    """What was recorded for this item, if it still describes the same bytes.

    Passing a fingerprint is the cache check on the analysis path: a file that
    has been edited since is a different picture and gets looked at again.
    Leaving it out is the read Review and search do, where the question is
    simply "what does the record say about this item?".
    """
    row = conn.execute("SELECT * FROM vision_results WHERE item_id=?", (item_id,)).fetchone()
    if row is None:
        return None
    if fingerprint is not None and row["fingerprint"] != (fingerprint or ""):
        return None
    return _from_row(row)


def _from_row(row: sqlite3.Row) -> StoredVision:
    return StoredVision(
        provider=row["provider"],
        model=row["model"],
        category=row["category"],
        caption=row["caption"],
        subjects=tuple(_json_list(row["subjects"])),
        tags=tuple(_json_list(row["tags"])),
        name_tokens=tuple(_json_list(row["name_tokens"])),
        visible_text=row["visible_text"],
        confidence=row["confidence"],
        created_at=row["created_at"],
    )


def vision_disagrees(stored: StoredVision | None, category: str) -> bool:
    """Whether the model thinks this belongs somewhere else than it is filed.

    Only a mapped image kind counts. "other" maps to nothing and is not a
    disagreement — it is the model saying it has no opinion.
    """
    if stored is None:
        return False
    mapped = CATEGORY_MAP.get(stored.category or "")
    return mapped is not None and mapped != category


def vision_for_items(
    conn: sqlite3.Connection, item_ids: list[int]
) -> dict[int, StoredVision]:
    """Every stored description for one page of rows, in one query.

    Review draws fifty rows at a time and each one may or may not have been
    looked at; asking per row would be fifty queries to decorate a page.
    """
    if not item_ids:
        return {}
    placeholders = ",".join("?" for _ in item_ids)
    rows = conn.execute(
        f"SELECT * FROM vision_results WHERE item_id IN ({placeholders})",  # noqa: S608
        item_ids,
    ).fetchall()
    return {int(row["item_id"]): _from_row(row) for row in rows}


def _as_answer(stored: StoredVision) -> VisionResult:
    """A stored row back in the shape `apply_vision` takes.

    The name tokens come back too, so re-analysing an unchanged file arrives at
    exactly the same filename it did the first time. `_named` only ever appends
    to a stem that says nothing, so replaying them cannot compound.
    """
    return VisionResult(
        category=stored.category,
        caption=stored.caption,
        subjects=stored.subjects,
        tags=stored.tags,
        filename_tokens=stored.name_tokens,
        visible_text=stored.visible_text,
        confidence=stored.confidence,
    )


def _json_list(payload: str | None) -> list[str]:
    try:
        value = json.loads(payload or "[]")
    except (TypeError, ValueError):
        return []
    return [str(item) for item in value] if isinstance(value, list) else []
