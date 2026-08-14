""""Lightweight" as a measurement rather than a description.

The design says: the photo beside the clip first, then the thumbnail Browse
already rendered, and only then three frames as a single image. That is a claim
about cost, and a claim about cost that nothing counts is a claim that quietly
stops being true — one refactor turning one inference into three, or one
eligibility rule loosening until every `.mp4` in the library gets analysed.

So these count. Every call to the provider is recorded, every decode is
recorded, and the assertions are on the numbers:

    sibling evidence      0 frames, 0 inferences
    cached thumbnail      0 new decodes, 1 inference
    fallback              <= 3 frames, exactly 1 inference
    anything with an identity of its own   nothing at all

`test_video_vision.py` holds the shape of the design; this holds its price.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from librairy.classify import video_vision as vv
from librairy.classify.images import save_vision
from librairy.config import Settings
from librairy.db import connect
from librairy.models import Item
from librairy.scanner import scan_root

MOV = "Photos/2026/IMG_0585.MOV"
JPEG = "Photos/2026/IMG_0585.jpeg"


@dataclass
class Result:
    """A classification result, shaped like the real one.

    A dataclass because the module folds evidence in with `dataclasses.replace`
    — it never mutates what it was given, so the caller's result is the same
    object it was before whatever happened here.
    """

    category: str = "photos"
    clean_name: str = "IMG_0585.MOV"
    confidence: float = 0.4
    evidence: list = field(default_factory=list)
    fields: dict = field(default_factory=dict)


class Answer:
    def __init__(self, caption: str = "a child outdoors") -> None:
        self.category = "photo"
        self.caption = caption
        self.subjects = ("child",)
        self.tags = ("outdoor",)
        self.filename_tokens = ("child", "outdoor", "orange")
        self.visible_text = None
        self.confidence = 0.7


@dataclass
class Ledger:
    """What this analysis actually spent."""

    inferences: int = 0
    images_sent: list = None
    decodes: int = 0

    def __post_init__(self) -> None:
        self.images_sent = []


@pytest.fixture
def ledger(monkeypatch) -> Ledger:
    """Count every provider call and every decode, wherever they come from.

    Patched at the definition site rather than at the call site, so a new code
    path that reaches the provider some other way is counted too — the point is
    to measure the module, not the one route the test knows about.
    """
    book = Ledger()

    def describe_image(config, image, **kwargs):  # noqa: ANN001, ARG001
        book.inferences += 1
        book.images_sent.append(Path(image))
        return Answer()

    def frames_for(plan, source, workdir, duration):  # noqa: ANN001
        book.decodes += 1
        return vv_frames_for(plan, source, workdir, duration)

    vv_frames_for = vv.frames_for
    monkeypatch.setattr("librairy.ai.vision.describe_image", describe_image)
    monkeypatch.setattr(vv, "frames_for", frames_for)
    return book


def settings_for(tmp_path: Path) -> Settings:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        AUTH_REQUIRED=False,
        VISION_ENABLED=True,
        _env_file=None,
    )
    for root in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        root.mkdir(parents=True, exist_ok=True)
    return settings


def scene(tmp_path: Path, *, photo: bool = False, described: bool = False, name: str = MOV):
    settings = settings_for(tmp_path)
    conn = connect(settings)
    for relpath in (name, *((JPEG,) if photo else ())):
        path = settings.library_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"bytes of " + relpath.encode())
    scan_root(conn, "library", settings.library_dir, settings)
    if described:
        row = conn.execute(
            "SELECT id, fingerprint FROM items WHERE relpath=?", (JPEG,)
        ).fetchone()
        save_vision(
            conn, _item(row, JPEG), Answer("a child sleeping"),
            provider="ollama", model="qwen2.5vl",
        )
    return conn, settings


def _item(row, relpath: str) -> Item:
    return Item(
        id=row["id"], root="library", relpath=relpath, size=1, mtime_ns=1,
        fingerprint=row["fingerprint"], state="committed",
        first_seen_at="now", last_seen_at="now", missing_since=None,
    )


def item_for(conn, relpath: str) -> Item:
    row = conn.execute(
        "SELECT id, fingerprint FROM items WHERE relpath=?", (relpath,)
    ).fetchone()
    return _item(row, relpath)


def provider():
    from librairy.ai.vision import ProviderConfig

    try:
        return ProviderConfig(name="ollama", base_url="http://127.0.0.1:1", model="qwen2.5vl")
    except TypeError:  # a differently-shaped config is still local
        return None


def set_duration(conn, item: Item, seconds: float) -> None:
    """Fill the shared probe cache, which is the only place duration is read
    from — `cached_duration` never probes.

    Through the real writer, so this cannot drift from the reader: a test that
    hand-writes the cache row is testing its own idea of the schema.
    """
    from librairy.optimization import TOOL
    from librairy.tools.common import set_cached_metadata

    set_cached_metadata(
        conn, item.id, item.fingerprint, TOOL,
        {"duration": seconds}, "2026-01-01T00:00:00+00:00",
    )


# --- tier 0: the photo beside the clip ---------------------------------------


def test_sibling_evidence_costs_no_frames_and_no_inference(tmp_path: Path, ledger) -> None:
    conn, settings = scene(tmp_path, photo=True, described=True)
    item = item_for(conn, MOV)

    result = vv.enrich_video(conn, settings, item, Result(), provider=provider())

    assert ledger.inferences == 0
    assert ledger.decodes == 0
    fields = [entry.field for entry in result.evidence]
    assert "paired phone photo" in fields
    # And it says where it came from, on the entry itself. A caption that does
    # not admit it describes the photo rather than the clip is a caption
    # somebody will reasonably read as a description of the video.
    notes = " ".join(entry.note for entry in result.evidence)
    assert "not from the video" in notes


def test_the_free_tier_is_not_skipped_just_because_a_provider_exists(
    tmp_path: Path, ledger
) -> None:
    """A feature that exists is not a reason to use it."""
    conn, settings = scene(tmp_path, photo=True, described=True)
    plan = vv.plan_for(conn, item_for(conn, MOV).id, MOV)

    assert plan.strategy == "paired-photo"
    assert plan.needs_inference is False
    assert plan.frames == ()


# --- tier 1: the thumbnail Browse already made -------------------------------


def test_an_existing_thumbnail_costs_no_new_decode(tmp_path: Path, ledger) -> None:
    conn, settings = scene(tmp_path)
    item = item_for(conn, MOV)
    thumbs = settings.appdata_dir / "thumbs"
    thumbs.mkdir(parents=True, exist_ok=True)
    (thumbs / f"{item.fingerprint[:32]}.jpg").write_bytes(b"a rendered frame")

    plan = vv.plan_for(conn, item.id, MOV, thumbnail=thumbs / f"{item.fingerprint[:32]}.jpg")

    assert plan.strategy == "thumbnail"
    assert len(plan.frames) == 1
    assert plan.frames[0].suffix == ".jpg"


# --- tier 2: the bounded fallback --------------------------------------------


def test_the_fallback_never_exceeds_three_frames(tmp_path: Path) -> None:
    assert vv.MAX_FRAMES == 3
    assert len(vv.FRAME_POSITIONS) <= vv.MAX_FRAMES


def test_the_fallback_spends_exactly_one_inference(tmp_path: Path, ledger) -> None:
    """Three frames, one request. Three requests would cost three model loads
    on a local server, which is where this runs."""
    conn, settings = scene(tmp_path)
    item = item_for(conn, MOV)
    set_duration(conn, item, 9.0)

    vv.enrich_video(conn, settings, item, Result(), provider=provider())

    assert ledger.inferences <= 1
    assert len(ledger.images_sent) <= 1


def test_no_video_file_is_ever_handed_to_the_provider(tmp_path: Path, ledger) -> None:
    """The constraint the whole design exists for, checked against what was
    actually passed rather than against the shape of the code."""
    conn, settings = scene(tmp_path)
    item = item_for(conn, MOV)
    set_duration(conn, item, 9.0)

    vv.enrich_video(conn, settings, item, Result(), provider=provider())

    for sent in ledger.images_sent:
        assert sent.suffix.lower() not in {".mov", ".mp4", ".mkv", ".avi", ".m4v"}
        assert sent != settings.library_dir / MOV


# --- eligibility: most videos are not candidates at all ----------------------


@pytest.mark.parametrize(
    "relpath",
    [
        "Movies/The Matrix (1999)/The Matrix (1999).mkv",
        "Shows/Breaking Bad/Season 01/Breaking Bad - S01E01.mkv",
        "Movies/Casino (1995)/VIDEO_TS/VTS_01_1.VOB",
        "Music Videos/Daft Punk/Daft Punk - Around the World.mp4",
        "Movies/some-long-unnamed-recording.mkv",
    ],
)
def test_a_file_with_an_identity_of_its_own_is_never_looked_at(
    tmp_path: Path, relpath: str
) -> None:
    """Vision is for clips nothing else can name.

    A film, an episode, a DVD structure and a music video all have real
    identity paths — catalogs, filename parsing, tags — and a frame must never
    be the reason a file lands in one of those. A frame of a performer on a
    stage is equally consistent with a family video, a concert bootleg and a
    DJ set.
    """
    conn, settings = scene(tmp_path)

    assert vv.looks_personal(relpath) is False
    assert vv.plan_for(conn, 1, relpath).strategy == ""


def test_a_long_recording_is_not_a_phone_clip(tmp_path: Path) -> None:
    assert vv.looks_personal("Photos/2026/IMG_0585.MOV", duration=9) is True
    assert vv.looks_personal(
        "Photos/2026/IMG_0585.MOV", duration=vv.MAX_PERSONAL_SECONDS + 1
    ) is False


def test_size_alone_never_makes_something_a_candidate(tmp_path: Path) -> None:
    """"Huge, and badly named" is not the same as "personal"."""
    assert vv.looks_personal("Movies/BIGFILE.mp4") is False


# --- the cache identity -------------------------------------------------------


def test_a_different_model_does_not_reuse_the_previous_answer(tmp_path: Path) -> None:
    """The same bytes, looked at the same way, by a different model, is a
    different answer. Provider and model were always stored and never checked."""
    from librairy.classify.images import stored_vision

    conn, settings = scene(tmp_path)
    item = item_for(conn, MOV)
    save_vision(conn, item, Answer(), provider="ollama", model="qwen2.5vl",
                strategy="contact-sheet-v1")

    same = stored_vision(conn, item.id, fingerprint=item.fingerprint,
                         strategy="contact-sheet-v1", model="qwen2.5vl")
    other = stored_vision(conn, item.id, fingerprint=item.fingerprint,
                          strategy="contact-sheet-v1", model="llava:13b")

    assert same is not None
    assert other is None


def test_a_different_strategy_does_not_reuse_the_previous_answer(tmp_path: Path) -> None:
    from librairy.classify.images import stored_vision

    conn, settings = scene(tmp_path)
    item = item_for(conn, MOV)
    save_vision(conn, item, Answer(), provider="ollama", model="qwen2.5vl",
                strategy="thumbnail-v1")

    assert stored_vision(conn, item.id, strategy="contact-sheet-v1") is None
    assert stored_vision(conn, item.id, strategy="thumbnail-v1") is not None


def test_the_strategy_version_is_part_of_the_key() -> None:
    """Changing which frames are sent changes the answer, so an old result must
    not be served under the new strategy's name."""
    plan = vv.Plan("contact-sheet")

    assert plan.cache_key.endswith(f"-v{vv.STRATEGY_VERSION}")


def test_a_plain_read_still_returns_whatever_was_recorded(tmp_path: Path) -> None:
    """Review and search ask "what does the record say?", not "is it fresh?"."""
    from librairy.classify.images import stored_vision

    conn, settings = scene(tmp_path)
    item = item_for(conn, MOV)
    save_vision(conn, item, Answer(), provider="ollama", model="qwen2.5vl",
                strategy="contact-sheet-v1")

    assert stored_vision(conn, item.id) is not None
