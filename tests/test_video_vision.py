"""Personal videos get a caption. No model ever sees a video.

Local vision on photos turned `IMG_1423.jpeg` into
`IMG_1423-child-outdoor-orange.jpeg`. Videos got nothing, and the one thing the
deterministic pass did know about `IMG_0585.MOV` — the extension — pointed at
Movies, which is exactly wrong for a nine-second clip of a dog.

The constraint that shapes the whole design is in the title, and the test that
holds it is `test_no_video_path_can_reach_a_provider`. Everything else here is
about spending as little as possible to get there: the photo beside the clip
first, then the thumbnail Browse already rendered, and only then three frames —
as one image, because one request with three frames costs far less on a local
model than three requests with one.
"""

from __future__ import annotations

import inspect
import shutil
import subprocess
from pathlib import Path

import pytest

from librairy.ai.vision import VISION_EXTENSIONS
from librairy.classify import video_vision as vv
from librairy.classify.images import named_with_vision, save_vision, stored_vision
from librairy.config import Settings
from librairy.db import connect
from librairy.models import Item
from librairy.scanner import scan_root


def code_of(obj) -> str:
    """Source with docstrings and comments removed.

    Several assertions below are about what the module *does*, and the module
    explains at length what it deliberately does not do — "no scene detection",
    "never `shell=True`". A test that reads prose is a test the prose can
    break, and one that passes because a comment was deleted is worse.
    """
    lines = []
    for line in inspect.getsource(obj).splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        lines.append(line)
    text = "\n".join(lines)
    parts = text.split('"""')
    return "".join(parts[::2])


MOV = "Photos/2026/IMG_0585.MOV"
JPEG = "Photos/2026/IMG_0585.jpeg"


def settings_for(tmp_path: Path) -> Settings:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        AUTH_REQUIRED=False,
        _env_file=None,
    )
    for root in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        root.mkdir(parents=True, exist_ok=True)
    return settings


class Result:
    """A classification result, shaped like the real one."""

    def __init__(self) -> None:
        self.category = "photos"
        self.clean_name = "IMG_0585.MOV"
        self.confidence = 0.4
        self.evidence: list = []
        self.fields: dict = {}


class Answer:
    """A vision reply, shaped like the real one."""

    def __init__(self, caption: str, tokens: tuple[str, ...] = ()) -> None:
        self.category = "photo"
        self.caption = caption
        self.subjects = ("child",)
        self.tags = ("outdoor",)
        self.filename_tokens = tokens or ("child", "outdoor", "orange")
        self.visible_text = None
        self.confidence = 0.7


def scene(tmp_path: Path, *, with_photo: bool = False, described: bool = False):
    settings = settings_for(tmp_path)
    conn = connect(settings)
    for relpath in (MOV, *( (JPEG,) if with_photo else () )):
        path = settings.library_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"bytes of " + relpath.encode())
    scan_root(conn, "library", settings.library_dir, settings)
    if described:
        row = conn.execute("SELECT id, fingerprint FROM items WHERE relpath=?", (JPEG,)).fetchone()
        save_vision(
            conn,
            Item(
                id=row["id"], root="library", relpath=JPEG, size=1, mtime_ns=1,
                fingerprint=row["fingerprint"], state="committed",
                first_seen_at="now", last_seen_at="now", missing_since=None,
            ),
            Answer("a child sleeping peacefully"),
            provider="ollama",
            model="qwen2.5vl",
        )
    return conn, settings


def item_id(conn, relpath: str) -> int:
    return conn.execute("SELECT id FROM items WHERE relpath=?", (relpath,)).fetchone()["id"]


# --- the constraint ----------------------------------------------------------------


def test_no_video_path_can_reach_a_provider(tmp_path: Path) -> None:
    """The guarantee, asserted as a property of the only function that
    materialises frames: it returns images, and it can never return `source`.

    A test that merely checks the happy path would pass on the day somebody
    adds a "just send the file, the model handles video now" branch.
    """
    conn, settings = scene(tmp_path)
    source = settings.library_dir / MOV
    plan = vv.plan_for(conn, item_id(conn, MOV), MOV, duration=9.0)

    frames = vv.frames_for(plan, source, tmp_path, 9.0)

    assert source not in frames
    for frame in frames:
        assert frame.suffix.lower() in VISION_EXTENSIONS


def test_the_module_never_hands_back_the_source_it_was_given() -> None:
    """Read as source, because the guarantee has to survive a change nobody
    ran this suite against."""
    body = code_of(vv.frames_for)

    assert "return source" not in body
    assert "return (source" not in body
    # The one return that produces frames returns the sheet, never the input.
    assert "return (sheet,)" in body


def test_the_frame_budget_does_not_grow_with_duration() -> None:
    """No scene detection, no keyframe analysis, no "a few more for long
    videos". Three is the ceiling and it is a constant."""
    assert vv.MAX_FRAMES == 3
    assert len(vv.FRAME_POSITIONS) == vv.MAX_FRAMES
    code = code_of(vv)
    for expensive in ("select=", "scene", "keyframe", "-map 0:v"):
        assert expensive not in code


def test_frames_are_extracted_with_argv_and_never_a_shell() -> None:
    code = code_of(vv)

    assert "shell=True" not in code
    assert "subprocess.run(" in code
    assert "timeout=" in code


# --- tier 0: the photo beside it ------------------------------------------------------


def test_a_described_photo_beside_the_clip_answers_for_free(tmp_path: Path) -> None:
    conn, _ = scene(tmp_path, with_photo=True, described=True)

    plan = vv.plan_for(conn, item_id(conn, MOV), MOV, duration=9.0)

    assert plan.strategy == "paired-photo"
    assert plan.needs_inference is False
    assert plan.sibling is not None
    assert plan.sibling.caption == "a child sleeping peacefully"


def test_the_paired_photo_is_context_and_never_identity(tmp_path: Path) -> None:
    """The still and the clip are not guaranteed to show the same thing.
    Saying they do is the kind of confident wrongness that makes people stop
    trusting captions."""
    conn, _ = scene(tmp_path, with_photo=True, described=True)
    plan = vv.plan_for(conn, item_id(conn, MOV), MOV, duration=9.0)

    entries = vv.sibling_evidence(plan.sibling)

    assert any("paired phone photo" in entry.field for entry in entries)
    hint = next(entry for entry in entries if entry.field == "visual hint")
    assert "not from the video" in hint.note
    assert hint.weight < 0.7, "supporting evidence, not an identity"


def test_an_undescribed_photo_beside_it_is_not_used(tmp_path: Path) -> None:
    """This tier exists to spend no inference at all. Asking for one here
    would make the cheap path the expensive one."""
    conn, _ = scene(tmp_path, with_photo=True, described=False)

    plan = vv.plan_for(conn, item_id(conn, MOV), MOV, duration=9.0)

    assert plan.strategy != "paired-photo"


def test_the_same_stem_in_another_folder_is_not_the_same_moment(tmp_path: Path) -> None:
    """A different camera's counter reaching the same number. The classifier
    learned this from seven phone-camera folders."""
    conn, settings = scene(tmp_path, with_photo=True, described=True)
    elsewhere = settings.library_dir / "Photos/2019/IMG_0585.jpeg"
    elsewhere.parent.mkdir(parents=True, exist_ok=True)
    elsewhere.write_bytes(b"a different photo entirely")
    scan_root(conn, "library", settings.library_dir, settings)

    found = vv.paired_photo(conn, item_id(conn, MOV), MOV)

    assert found is not None
    assert found.relpath == JPEG


# --- tier 1 and 2 --------------------------------------------------------------------


def test_an_existing_thumbnail_is_preferred_over_new_extraction(tmp_path: Path) -> None:
    conn, _ = scene(tmp_path)
    thumb = tmp_path / "thumb.jpg"
    thumb.write_bytes(b"\xff\xd8\xff a jpeg")

    plan = vv.plan_for(conn, item_id(conn, MOV), MOV, thumbnail=thumb, duration=9.0)

    assert plan.strategy == "thumbnail"
    assert plan.frames == (thumb,)


def test_an_empty_thumbnail_is_not_a_thumbnail(tmp_path: Path) -> None:
    conn, _ = scene(tmp_path)
    thumb = tmp_path / "thumb.jpg"
    thumb.touch()

    plan = vv.plan_for(conn, item_id(conn, MOV), MOV, thumbnail=thumb, duration=9.0)

    assert plan.strategy == "contact-sheet"


def test_without_a_duration_nothing_is_extracted(tmp_path: Path) -> None:
    """Guessing at timestamps in a file of unknown length is how you get three
    black frames and one inference spent on them."""
    conn, _ = scene(tmp_path)

    plan = vv.plan_for(conn, item_id(conn, MOV), MOV, duration=None)

    assert plan.strategy == ""
    assert plan.needs_inference is False


def test_one_request_is_preferred_over_three(tmp_path: Path) -> None:
    """Three frames, one image, one inference. Three separate calls would be
    three model loads and three prompt encodings on a local provider."""
    conn, _ = scene(tmp_path)

    plan = vv.plan_for(conn, item_id(conn, MOV), MOV, duration=30.0)

    assert plan.strategy == "contact-sheet"
    assert "as one image" in plan.reason


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_a_real_contact_sheet_is_one_image_from_three_frames(tmp_path: Path) -> None:
    """A generated fixture, not a file from anybody's library."""
    source = tmp_path / "clip.mp4"
    subprocess.run(  # noqa: S603
        [
            "ffmpeg", "-nostdin", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=duration=6:size=320x240:rate=10",
            str(source),
        ],
        check=True,
        timeout=60,
    )

    sheet = vv.contact_sheet(source, tmp_path / "sheet.jpg", 6.0)

    assert sheet is not None
    assert sheet.is_file()
    assert sheet.suffix == ".jpg"
    # One file, not three: the stills are cleaned up after stacking.
    assert not list(tmp_path.glob("sheet-*.jpg"))


# --- what a frame is worth -------------------------------------------------------------


def test_a_frame_is_labelled_a_hint_and_not_the_clip() -> None:
    """A single still of a kitchen says nothing about the ninety seconds after
    it, and a caption presented as identity would be believed."""
    entries = vv.frame_evidence("dog running in a yard", "thumbnail")

    assert entries[0].field == "visual hint"
    assert "may not describe the whole clip" in entries[0].note


def test_the_evidence_says_how_many_frames_it_read() -> None:
    one = vv.frame_evidence("x", "thumbnail")[0].note
    three = vv.frame_evidence("x", "contact-sheet")[0].note

    assert "one frame" in one
    assert "3 frames" in three


# --- caching ---------------------------------------------------------------------------


def test_an_unchanged_video_reuses_its_answer(tmp_path: Path) -> None:
    conn, _ = scene(tmp_path)
    row = conn.execute("SELECT id, fingerprint FROM items WHERE relpath=?", (MOV,)).fetchone()
    item = Item(
                id=row["id"], root="library", relpath=MOV, size=1, mtime_ns=1,
                fingerprint=row["fingerprint"], state="committed",
                first_seen_at="now", last_seen_at="now", missing_since=None,
            )
    save_vision(
        conn, item, Answer("a dog in a yard"),
        provider="ollama", model="qwen2.5vl", strategy="thumbnail-v1",
    )

    found = stored_vision(
        conn, item.id, fingerprint=item.fingerprint, strategy="thumbnail-v1"
    )

    assert found is not None
    assert found.caption == "a dog in a yard"


def test_a_changed_video_does_not(tmp_path: Path) -> None:
    conn, _ = scene(tmp_path)
    row = conn.execute("SELECT id, fingerprint FROM items WHERE relpath=?", (MOV,)).fetchone()
    item = Item(
                id=row["id"], root="library", relpath=MOV, size=1, mtime_ns=1,
                fingerprint=row["fingerprint"], state="committed",
                first_seen_at="now", last_seen_at="now", missing_since=None,
            )
    save_vision(
        conn, item, Answer("a dog"), provider="ollama", model="m", strategy="thumbnail-v1"
    )

    assert stored_vision(conn, item.id, fingerprint="different", strategy="thumbnail-v1") is None


def test_a_different_frame_strategy_does_not_reuse_the_old_answer(tmp_path: Path) -> None:
    """An answer read off one thumbnail is not the answer three frames would
    have given."""
    conn, _ = scene(tmp_path)
    row = conn.execute("SELECT id, fingerprint FROM items WHERE relpath=?", (MOV,)).fetchone()
    item = Item(
                id=row["id"], root="library", relpath=MOV, size=1, mtime_ns=1,
                fingerprint=row["fingerprint"], state="committed",
                first_seen_at="now", last_seen_at="now", missing_since=None,
            )
    save_vision(
        conn, item, Answer("a dog"), provider="ollama", model="m", strategy="thumbnail-v1"
    )

    assert stored_vision(
        conn, item.id, fingerprint=item.fingerprint, strategy="contact-sheet-v1"
    ) is None


def test_the_strategy_version_is_part_of_the_cache_key() -> None:
    plan = vv.Plan("contact-sheet")

    assert plan.cache_key == f"contact-sheet-v{vv.STRATEGY_VERSION}"


# --- what a frame must never decide -----------------------------------------------------


def test_a_phone_mov_is_not_a_movie_because_of_its_extension(tmp_path: Path) -> None:
    assert vv.looks_personal("Photos/2026/IMG_0585.MOV", 9.0)
    assert vv.looks_personal("Photos/2026/VID_20260514_101112.mp4", 30.0)


def test_a_long_video_is_not_a_phone_moment(tmp_path: Path) -> None:
    """Something with an identity a catalog should be answering for, and
    decoding a frame out of a 40 GB remux is the opposite of lightweight."""
    assert not vv.looks_personal("Movies/The Matrix (1999)/The Matrix.mkv", 8100.0)
    assert not vv.looks_personal("Photos/2026/IMG_0585.MOV", 9000.0)


def test_a_music_video_is_not_classified_by_a_frame() -> None:
    """A frame showing a performer is equally consistent with a family video,
    a concert bootleg and a DJ music video. Music Video classification is
    filename, tags and catalogs — and this module cannot reach any of it."""
    code = code_of(vv)

    for forbidden in ("musicvideo", "music_video", "tmdb", "musicbrainz"):
        assert forbidden not in code
    assert not vv.looks_personal("Music Videos/House/Daft Punk/One More Time.mp4", 320.0)


def test_it_cannot_produce_a_category_or_a_destination() -> None:
    """The model returns words. Nothing here builds a path — the same rule the
    photo path has lived under since it was written."""
    code = code_of(vv)

    for forbidden in ("render_destination", "dest_relpath", "CATEGORY_MAP"):
        assert forbidden not in code


# --- naming ------------------------------------------------------------------------------


def test_a_proposed_name_uses_the_shared_naming_policy() -> None:
    """One sanitizer. Two would drift, and the day they disagree is the day a
    photo and the clip beside it are named by different rules."""
    named = named_with_vision("IMG_1423.mov", Answer("x", ("child", "outdoor", "orange")))

    assert named == "IMG_1423-child-outdoor-orange.mov"


def test_the_camera_identifier_survives() -> None:
    """The camera's sequence number is how you find that clip again on the
    phone."""
    named = named_with_vision("IMG_0585.MOV", Answer("x", ("dog", "yard")))

    assert "0585" in named


def test_a_name_somebody_chose_is_never_overwritten() -> None:
    named = named_with_vision("Emma birthday.mov", Answer("x", ("cake", "table")))

    assert named == "Emma birthday.mov"


def test_captions_stay_descriptive_rather_than_inventing_identity() -> None:
    """`child-outdoors-orange-shirt` is what was in shot. "Sarah's fifth
    birthday at Grandma's house" is three claims no frame supports."""
    entries = vv.frame_evidence("child outdoors in an orange shirt", "thumbnail")

    assert entries[0].source == "vision"
    assert entries[0].weight <= 0.5


# --- wiring ------------------------------------------------------------------------------


def test_the_classifier_offers_a_video_the_same_help_it_offers_a_photo() -> None:
    """An unused module is not a feature. The cascade calls this."""
    from librairy import classify

    body = inspect.getsource(classify._enriched)

    assert "enrich_video(" in body
    assert body.index("enrich_with_vision(") < body.index("enrich_video(")


def test_nothing_happens_when_vision_is_switched_off(tmp_path: Path) -> None:
    conn, settings = scene(tmp_path)
    row = conn.execute("SELECT id, fingerprint FROM items WHERE relpath=?", (MOV,)).fetchone()
    item = Item(
        id=row["id"], root="library", relpath=MOV, size=1, mtime_ns=1,
        fingerprint=row["fingerprint"], state="committed",
        first_seen_at="now", last_seen_at="now", missing_since=None,
    )
    before = Result()

    after = vv.enrich_video(conn, settings, item, before)

    assert after is before


def test_no_local_provider_skips_cleanly_and_the_classifier_continues(
    tmp_path: Path, monkeypatch
) -> None:
    """The deterministic pass keeps its answer. An enrichment that can break
    analysis is not one."""
    conn, settings = scene(tmp_path)
    monkeypatch.setattr(settings, "vision_enabled", True, raising=False)
    monkeypatch.setattr(
        "librairy.classify.images.local_vision_provider", lambda *a, **k: None
    )
    row = conn.execute("SELECT id, fingerprint FROM items WHERE relpath=?", (MOV,)).fetchone()
    item = Item(
        id=row["id"], root="library", relpath=MOV, size=1, mtime_ns=1,
        fingerprint=row["fingerprint"], state="committed",
        first_seen_at="now", last_seen_at="now", missing_since=None,
    )
    before = Result()

    after = vv.enrich_video(conn, settings, item, before)

    assert after is before


def test_a_cloud_provider_is_never_used_for_personal_video_frames() -> None:
    """Frames of somebody's family are exactly the thing that must not leave
    the machine because a cloud provider happened to be configured for
    filenames. The picker only ever offers local providers, and
    `describe_image` refuses the rest outright — this module adds no way in."""
    picker = inspect.getsource(
        __import__("librairy.classify.images", fromlist=["x"]).local_vision_provider
    )
    assert "is_local" in picker
    assert '{"lmstudio", "ollama"}' in picker

    code = code_of(vv)
    assert "local_vision_provider" in code
    for cloud in ("openai", "anthropic", "gemini", "api_key"):
        assert cloud not in code.lower()

