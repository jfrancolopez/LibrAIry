"""Image understanding: what the model is asked, and what it is allowed to do.

Two halves, deliberately kept apart, mirroring the code:

* parsing and transport — a model can return anything, and none of it may
  raise on the analysis path;
* the rules — a description is evidence, and evidence does not file files.

The transport is always monkeypatched. `conftest.py` refuses outbound sockets,
which is what stopped an earlier test in this project from quietly passing
against a real LM Studio server on the author's desk and nowhere else.
"""

from __future__ import annotations

import json
import shutil
import struct
import zlib
from pathlib import Path

import pytest

from librairy.ai.base import ProviderConfig
from librairy.ai.vision import (
    VISION_PROMPT,
    VisionResult,
    describe_image,
    encoded_image,
    validate_vision_response,
)
from librairy.classify import analyze_items
from librairy.classify.images import (
    AGREEMENT_BONUS,
    VISION_CONFIDENCE_CAP,
    apply_vision,
    says_nothing,
    stored_vision,
    vision_wanted,
)
from librairy.config import Settings
from librairy.db import connect
from librairy.models import Item
from librairy.scanner import scan_root

LOCAL = ProviderConfig("lmstudio", "lmstudio", "http://10.0.0.5:1234", "a-model", True, True)
CLOUD = ProviderConfig("openai", "openai", None, "gpt-4o-mini", True, False)

FULL_ANSWER = {
    "category": "photo",
    "caption": "A baby sitting on a couch holding an orange cat.",
    "subjects": ["baby", "cat"],
    "tags": ["family", "indoor", "cat"],
    "visible_text": None,
    "filename_tokens": ["baby", "orange-cat"],
    "confidence": 0.89,
}


# --------------------------------------------------------------------------
# Fixtures


def _settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "APPDATA_DIR": tmp_path / "appdata",
        "INBOX_DIR": tmp_path / "inbox",
        "LIBRARY_DIR": tmp_path / "library",
        "QUARANTINE_DIR": tmp_path / "quarantine",
        "FILE_STABILITY_SECONDS": 0,
        # Ollama blank so nothing in here reaches for a text provider; these
        # tests are about images, and conftest refuses the socket anyway.
        "OLLAMA_HOST": "",
        "LMSTUDIO_HOST": "http://10.0.0.5:1234",
        "LMSTUDIO_MODEL": "a-model",
        "VISION_ENABLED": True,
    }
    settings = Settings(**{**values, **overrides}, _env_file=None)
    for directory in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        directory.mkdir(parents=True, exist_ok=True)
    return settings


def _png(path: Path, *, width: int = 8, height: int = 8, alpha: int = 255) -> Path:
    """A real PNG, written by hand. Black pixels at whatever alpha is asked for.

    Hand-rolled rather than fixtured because the alpha channel is the point of
    one of these tests and a checked-in binary would hide it.
    """
    raw = b"".join(
        b"\x00" + bytes([0, 0, 0, alpha]) * width for _ in range(height)
    )

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    return path


def _item(conn, relpath: str) -> Item:
    row = conn.execute("SELECT * FROM items WHERE relpath=?", (relpath,)).fetchone()
    return Item(
        id=row["id"],
        root=row["root"],
        relpath=row["relpath"],
        size=row["size"],
        mtime_ns=row["mtime_ns"],
        fingerprint=row["fingerprint"],
        state=row["state"],
        first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"],
        missing_since=row["missing_since"],
    )


def _answered(monkeypatch, payload: dict | None, *, calls: list | None = None):
    """Make the model reply with `payload`, without opening a socket."""

    def fake(config, path, **kwargs):  # noqa: ANN001, ANN003
        if calls is not None:
            calls.append((config.name, path.name, kwargs.get("model")))
        return None if payload is None else VisionResult.model_validate(payload)

    monkeypatch.setattr("librairy.classify.images.describe_image", fake)


# --------------------------------------------------------------------------
# What comes back from the model


def test_valid_json_becomes_a_result() -> None:
    result = validate_vision_response(json.dumps(FULL_ANSWER))

    assert result is not None
    assert result.category == "photo"
    assert result.caption.startswith("A baby sitting")
    assert result.subjects == ("baby", "cat")
    assert result.confidence == 0.89


@pytest.mark.parametrize(
    "reply",
    [
        "",
        "I am afraid I cannot look at images.",
        "{not json at all",
        "[1, 2, 3]",
        "{}",
    ],
)
def test_unusable_replies_are_none_not_exceptions(reply: str) -> None:
    """Every one of these has to be survivable: this runs inside a scan."""
    assert validate_vision_response(reply) is None


def test_json_inside_prose_and_a_code_fence_is_still_found() -> None:
    reply = f"Sure, here you go:\n```json\n{json.dumps(FULL_ANSWER)}\n```\nHope that helps!"

    assert validate_vision_response(reply).caption.startswith("A baby")


def test_a_partial_answer_is_kept() -> None:
    """A small model that manages a caption and nothing else has still helped."""
    result = validate_vision_response('{"caption": "A dog on a beach."}')

    assert result is not None
    assert result.caption == "A dog on a beach."
    assert result.subjects == ()
    assert result.confidence is None


def test_a_comma_separated_string_is_read_as_a_list() -> None:
    """Models answer "baby, cat" where a list was asked for, constantly."""
    result = validate_vision_response('{"caption": "x", "tags": "family, indoor, cat"}')

    assert result.tags == ("family", "indoor", "cat")


def test_runaway_fields_are_trimmed_not_trusted() -> None:
    result = validate_vision_response(
        json.dumps({"caption": "c" * 5000, "tags": [f"tag{n}" for n in range(200)]})
    )

    assert len(result.caption) == 300
    assert len(result.tags) == 12


def test_an_unknown_category_is_dropped_rather_than_invented() -> None:
    result = validate_vision_response('{"caption": "x", "category": "family-holiday"}')

    assert result.category is None


def test_ocr_text_is_kept_apart_from_the_caption() -> None:
    """The description and what the image actually says are two things.

    Merging them is the obvious shortcut and it destroys both: the caption
    stops being readable and the text stops being quotable.
    """
    result = validate_vision_response(
        json.dumps(
            {
                "category": "screenshot",
                "caption": "A screenshot of a phone's Wi-Fi settings.",
                "visible_text": "Wi-Fi\nMY NETWORKS\nCasaFranco 5GHz",
            }
        )
    )

    assert result.caption == "A screenshot of a phone's Wi-Fi settings."
    assert result.visible_text == "Wi-Fi\nMY NETWORKS\nCasaFranco 5GHz"
    assert "CasaFranco" not in result.caption
    # The caption collapses to one line; OCR keeps its shape, which is most of
    # what makes a receipt or a form legible.
    assert "\n" in result.visible_text


def test_a_model_cannot_return_a_path() -> None:
    """There is no field for one, and anything path-shaped is dropped."""
    result = validate_vision_response(
        json.dumps(
            {
                "caption": "x",
                "dest_relpath": "../../etc/passwd",
                "filename_tokens": ["../../etc/passwd", "C:\\Windows", ".ssh", "holiday"],
            }
        )
    )

    assert result.filename_tokens == ("holiday",)
    assert not hasattr(result, "dest_relpath")


def test_the_prompt_forbids_identifying_people() -> None:
    """The only enforcement there can be, so it must at least be present."""
    assert "Never name or identify a person" in VISION_PROMPT
    for forbidden in ("ethnicity", "health", "religion", "politics"):
        assert forbidden in VISION_PROMPT
    assert '"a man", "two people", "a child" is right' in VISION_PROMPT


# --------------------------------------------------------------------------
# Transport


def test_a_cloud_provider_is_never_asked(tmp_path: Path, monkeypatch) -> None:
    """Not an opt-in. There is no path from an image to a cloud provider."""
    asked: list[str] = []
    monkeypatch.setattr(
        "librairy.ai.vision._post", lambda *a, **k: asked.append("asked") or {}
    )
    monkeypatch.setattr("librairy.ai.vision.encoded_image", lambda *a, **k: "Zm9v")

    assert describe_image(CLOUD, _png(tmp_path / "a.png"), timeout=5) is None
    assert asked == []


def test_an_unsupported_extension_is_skipped_before_any_work(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "librairy.ai.vision.encoded_image",
        lambda *a, **k: pytest.fail("should not have tried to decode a HEIC"),
    )
    heic = tmp_path / "photo.heic"
    heic.write_bytes(b"not really a heic")

    assert describe_image(LOCAL, heic, timeout=5) is None


def test_an_unreachable_provider_returns_none(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("librairy.ai.vision.encoded_image", lambda *a, **k: "Zm9v")

    def refuse(*args, **kwargs):  # noqa: ANN002, ANN003
        raise OSError("connection refused")

    monkeypatch.setattr("librairy.ai.vision.request.urlopen", refuse)

    assert describe_image(LOCAL, _png(tmp_path / "a.png"), timeout=5) is None


def test_lmstudio_is_sent_the_image_as_a_data_uri(tmp_path: Path, monkeypatch) -> None:
    sent: dict = {}
    monkeypatch.setattr("librairy.ai.vision.encoded_image", lambda *a, **k: "Zm9v")
    monkeypatch.setattr(
        "librairy.ai.vision._post",
        lambda url, body, timeout, name: sent.update(url=url, body=body)
        or {"choices": [{"message": {"content": json.dumps(FULL_ANSWER)}}]},
    )

    result = describe_image(LOCAL, _png(tmp_path / "a.png"), timeout=5, model="vision-model")

    assert result.caption.startswith("A baby")
    assert sent["url"] == "http://10.0.0.5:1234/v1/chat/completions"
    assert sent["body"]["model"] == "vision-model", "the vision model overrides the chat one"
    content = sent["body"]["messages"][0]["content"]
    assert content[0]["text"] == VISION_PROMPT
    assert content[1]["image_url"]["url"] == "data:image/jpeg;base64,Zm9v"


def test_ollama_is_sent_the_image_in_its_own_shape(tmp_path: Path, monkeypatch) -> None:
    sent: dict = {}
    ollama = ProviderConfig("ollama-primary", "ollama", "http://10.0.0.6:11434", "m", True, True)
    monkeypatch.setattr("librairy.ai.vision.encoded_image", lambda *a, **k: "Zm9v")
    monkeypatch.setattr(
        "librairy.ai.vision._post",
        lambda url, body, timeout, name: sent.update(url=url, body=body)
        or {"response": json.dumps(FULL_ANSWER)},
    )

    assert describe_image(ollama, _png(tmp_path / "a.png"), timeout=5) is not None
    assert sent["url"] == "http://10.0.0.6:11434/api/generate"
    assert sent["body"]["images"] == ["Zm9v"]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg to scale an image")
def test_transparency_is_composited_onto_white_not_black(tmp_path: Path) -> None:
    """ffmpeg flattens alpha onto black, and most screenshots have alpha.

    Measured, not theorised: a fully transparent PNG reached the model as a
    solid black rectangle, which it accurately described as one. The check is
    on the bytes, because this failure is invisible from anywhere else.
    """
    import base64

    clear = _png(tmp_path / "clear.png", width=64, height=64, alpha=0)
    encoded = encoded_image(clear, max_edge=64)

    assert encoded is not None
    jpeg = base64.b64decode(encoded)
    # A 64x64 JPEG of one flat colour is tiny; what it must not be is black.
    # ffmpeg's own decoder settles it rather than a pixel library this project
    # does not have.
    out = tmp_path / "check.ppm"
    import subprocess

    (tmp_path / "in.jpg").write_bytes(jpeg)
    subprocess.run(  # noqa: S603
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(tmp_path / "in.jpg"),
         "-vf", "scale=1:1", "-f", "rawvideo", "-pix_fmt", "rgb24", str(out)],
        check=True, capture_output=True, timeout=30,
    )
    red, green, blue = out.read_bytes()[:3]
    assert min(red, green, blue) > 200, f"transparent PNG flattened to ({red},{green},{blue})"


# --------------------------------------------------------------------------
# When it runs


def test_disabled_means_nothing_is_asked() -> None:
    settings = Settings(VISION_ENABLED=False, _env_file=None)

    assert vision_wanted(settings, "holiday.jpg", 0.3) is False


def test_uncertain_mode_only_looks_below_the_threshold() -> None:
    settings = Settings(
        VISION_ENABLED=True, VISION_MODE="uncertain", CONFIDENCE_THRESHOLD=0.8, _env_file=None
    )

    assert vision_wanted(settings, "holiday.jpg", 0.3) is True
    # And this is why "all" is the default once the feature is on: an ordinary
    # photo scores 0.85 from its extension alone, so "uncertain" would skip
    # essentially every photo in a photo library.
    assert vision_wanted(settings, "holiday.jpg", 0.85) is False


def test_all_mode_looks_at_a_confident_photo_too() -> None:
    settings = Settings(VISION_ENABLED=True, VISION_MODE="all", _env_file=None)

    assert vision_wanted(settings, "holiday.jpg", 0.95) is True


def test_only_decodable_images_are_considered() -> None:
    settings = Settings(VISION_ENABLED=True, _env_file=None)

    assert vision_wanted(settings, "a.jpg", 0.1) is True
    assert vision_wanted(settings, "a.HEIC", 0.1) is False
    assert vision_wanted(settings, "a.mkv", 0.1) is False
    assert vision_wanted(settings, "VIDEO_TS/VTS_01_1.VOB", 0.1) is False


def test_an_invalid_mode_is_refused_at_startup() -> None:
    with pytest.raises(ValueError, match="vision mode"):
        Settings(VISION_MODE="sometimes", _env_file=None)


# --------------------------------------------------------------------------
# What it is allowed to change


def _result(tmp_path: Path, relpath: str, **overrides):
    from librairy.classify.heuristics import HeuristicResult
    from librairy.models import EvidenceEntry

    base = {
        "category": "photos",
        "clean_name": Path(relpath).name,
        "dest_relpath": "Photos/2024/Unsorted/x.jpg",
        "confidence": 0.85,
        "evidence": (EvidenceEntry("heuristic", "category", "image extension", 0.85),),
        "fields": {"clean_name": Path(relpath).name, "year": 2024, "event": "Unsorted"},
    }
    merged = {**base, **overrides}
    # The fields carry the filename the destination is rendered from, so a
    # test that overrides clean_name has to override both or it is testing a
    # state the classifiers never produce.
    merged["fields"] = {**merged["fields"], "clean_name": merged["clean_name"]}
    return HeuristicResult(**merged)


def _apply(tmp_path: Path, relpath: str, payload: dict, **overrides):
    settings = _settings(tmp_path)
    item = Item(1, "inbox", relpath, 10, 0, "fp", "proposed", "now", "now", None)
    return apply_vision(
        settings,
        item,
        _result(tmp_path, relpath, **overrides),
        VisionResult.model_validate(payload),
        "a-model",
    )


def test_a_meaningless_filename_gains_the_words(tmp_path: Path) -> None:
    out = _apply(tmp_path, "IMG_4821.jpg", FULL_ANSWER, clean_name="IMG_4821.jpg")

    assert out.clean_name == "IMG_4821-baby-orange-cat.jpg"
    assert out.fields["clean_name"] == out.clean_name


def test_a_filename_a_person_wrote_is_left_alone(tmp_path: Path) -> None:
    out = _apply(tmp_path, "wedding-day.jpg", FULL_ANSWER, clean_name="wedding-day.jpg")

    assert out.clean_name == "wedding-day.jpg"


def test_a_name_that_is_only_a_uuid_is_replaced_not_prefixed(tmp_path: Path) -> None:
    """36 characters of hex in front of "baby-orange-cat" is worse than either
    half. A UUID is not a disambiguator anybody can use, and the executor
    already refuses to overwrite anything."""
    name = "A2F98891-E89A-40A4-803A-31ECD1F1A488.jpeg"
    out = _apply(tmp_path, name, FULL_ANSWER, clean_name=name)

    assert out.clean_name == "baby-orange-cat.jpeg"


def test_a_number_paired_with_a_uuid_is_noise_too(tmp_path: Path) -> None:
    """iMessage's other shape, straight off the live inbox."""
    name = "78726114145__D68BA48A-94F5-4023-8D03-F6400AD555F3.jpeg"
    out = _apply(tmp_path, name, FULL_ANSWER, clean_name=name)

    assert out.clean_name == "baby-orange-cat.jpeg"


def test_a_camera_sequence_number_is_kept_as_a_prefix(tmp_path: Path) -> None:
    """Unlike a UUID: IMG_4821 is how you find that photo again on the phone."""
    out = _apply(tmp_path, "IMG_4821.jpg", FULL_ANSWER, clean_name="IMG_4821.jpg")

    assert out.clean_name == "IMG_4821-baby-orange-cat.jpg"


def test_a_capture_timestamp_survives_the_rename(tmp_path: Path) -> None:
    """The date is what makes a photo folder sort. It is added to, not replaced."""
    out = _apply(
        tmp_path, "IMG_20240612_101112.jpg", FULL_ANSWER, clean_name="IMG-20240612-101112.jpg"
    )

    assert out.clean_name == "IMG-20240612-101112-baby-orange-cat.jpg"


def test_a_uuid_says_nothing_and_a_word_says_something() -> None:
    assert says_nothing("01B583D3-1D28-4B3A-A5DD-9471447CFA27")
    assert says_nothing("IMG_4821")
    assert says_nothing("00093")
    assert says_nothing("PXL-20240612-101112")
    assert not says_nothing("wedding-day")
    assert not says_nothing("IMG-holiday")


def test_a_uuid_buried_in_a_longer_name_still_says_nothing() -> None:
    """Found live. Split on separators a UUID becomes seven chunks, three of
    them four hex characters — "123F" is indistinguishable from a word at that
    length, so the whole stem read as informative and the description was
    never added."""
    assert says_nothing("IMG_1423-0373923B-123F-4ABF-9B6E-2229413CEED4")
    assert says_nothing("78726114145__D68BA48A-94F5-4023-8D03-F6400AD555F3")
    assert not says_nothing("holiday-0373923B-123F-4ABF-9B6E-2229413CEED4")


def test_filename_tokens_go_through_the_sanitizer(tmp_path: Path) -> None:
    out = _apply(
        tmp_path,
        "IMG_1.jpg",
        {"caption": "x", "filename_tokens": ["Bébé & Cat!!", "  spaced  out  "]},
        clean_name="IMG_1.jpg",
    )

    assert out.clean_name == "IMG_1-bébé-and-cat-spaced-out.jpg"
    assert "&" not in out.clean_name
    assert "!" not in out.clean_name
    assert " " not in out.clean_name


def test_agreement_raises_the_score_a_little(tmp_path: Path) -> None:
    out = _apply(tmp_path, "IMG_1.jpg", FULL_ANSWER)

    assert out.confidence == pytest.approx(0.85 + AGREEMENT_BONUS)
    assert out.confidence <= VISION_CONFIDENCE_CAP


def test_agreement_cannot_push_past_the_ceiling(tmp_path: Path) -> None:
    out = _apply(tmp_path, "IMG_1.jpg", FULL_ANSWER, confidence=0.91)

    assert out.confidence == VISION_CONFIDENCE_CAP


def test_disagreement_changes_nothing_about_the_score_or_the_category(tmp_path: Path) -> None:
    """The one rule that keeps a caption from reorganising a photo library."""
    out = _apply(
        tmp_path,
        "IMG_1.jpg",
        {"category": "receipt", "caption": "A supermarket receipt."},
        confidence=0.85,
    )

    assert out.category == "photos", "vision never re-files anything"
    assert out.confidence == 0.85


def test_a_sidecar_held_below_the_threshold_stays_there(tmp_path: Path) -> None:
    """cover.jpg beside an album is deliberately 0.4. Vision must not rescue it."""
    out = _apply(
        tmp_path,
        "cover.jpg",
        FULL_ANSWER,
        category="misc",
        confidence=0.4,
        clean_name="cover.jpg",
    )

    assert out.confidence == 0.4
    assert out.category == "misc"


def test_what_it_saw_is_recorded_as_evidence(tmp_path: Path) -> None:
    out = _apply(tmp_path, "IMG_1.jpg", FULL_ANSWER)
    entry = out.evidence[-1]

    assert entry.source == "vision"
    assert "a-model" in entry.detail
    assert "orange cat" in entry.detail


def test_a_disagreement_says_so_in_the_evidence(tmp_path: Path) -> None:
    out = _apply(
        tmp_path, "IMG_1.jpg", {"category": "receipt", "caption": "A supermarket receipt."}
    )

    assert "looks more like receipt than photos" in out.evidence[-1].detail


# --------------------------------------------------------------------------
# End to end through a real analyze


def _scanned(tmp_path: Path, name: str = "IMG_4821.jpg", **overrides):
    settings = _settings(tmp_path, **overrides)
    _png(settings.inbox_dir / name)
    conn = connect(settings)
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    return conn, settings


def test_analysis_carries_on_when_vision_fails(tmp_path: Path, monkeypatch) -> None:
    """The whole point of calling it an enrichment."""
    conn, settings = _scanned(tmp_path)
    _answered(monkeypatch, None)

    summary = analyze_items(conn, settings)

    assert summary.analyzed == 1
    row = conn.execute("SELECT * FROM proposals").fetchone()
    assert row["category"] == "photos"
    assert row["dest_relpath"], "the file still got a destination"
    assert stored_vision(conn, row["item_id"]) is None


def test_analysis_carries_on_when_vision_raises(tmp_path: Path, monkeypatch) -> None:
    conn, settings = _scanned(tmp_path)

    def explode(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("the vision tower fell over")

    monkeypatch.setattr("librairy.classify.images.describe_image", explode)

    assert analyze_items(conn, settings).analyzed == 1
    assert conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 1


def test_a_photo_is_described_and_renamed(tmp_path: Path, monkeypatch) -> None:
    conn, settings = _scanned(tmp_path)
    _answered(monkeypatch, FULL_ANSWER)

    analyze_items(conn, settings)

    row = conn.execute("SELECT * FROM proposals").fetchone()
    assert row["clean_name"] == "IMG_4821-baby-orange-cat.jpg"
    seen = stored_vision(conn, row["item_id"])
    assert seen.caption.startswith("A baby")
    assert seen.subjects == ("baby", "cat")
    assert seen.model == "a-model"


def test_a_screenshot_is_renamed_too(tmp_path: Path, monkeypatch) -> None:
    """The screenshot heuristic keeps the real filename in the fields and a
    group label in clean_name, so reading clean_name meant screenshots were
    the one kind of image that never gained a description — by accident."""
    conn, settings = _scanned(tmp_path, name="Screenshot 2025-04-17 at 9.30.49 AM.png")
    _answered(
        monkeypatch,
        {"caption": "An artist biography.", "filename_tokens": ["micah-edwards", "music"]},
    )

    analyze_items(conn, settings)

    row = conn.execute("SELECT * FROM proposals").fetchone()
    assert row["dest_relpath"].endswith("screenshot-2025-04-17-093049-micah-edwards-music.png")
    assert row["clean_name"] == "Screenshots", "the group label is not a filename"


def test_a_screenshot_keeps_its_text(tmp_path: Path, monkeypatch) -> None:
    conn, settings = _scanned(tmp_path, name="Screenshot 2024-06-12 101112.png")
    _answered(
        monkeypatch,
        {
            "category": "screenshot",
            "caption": "A phone's Wi-Fi settings screen.",
            "tags": ["wifi", "settings"],
            "visible_text": "Wi-Fi\nMY NETWORKS\nCasaFranco 5GHz",
            "confidence": 0.94,
        },
    )

    analyze_items(conn, settings)

    row = conn.execute("SELECT * FROM proposals").fetchone()
    seen = stored_vision(conn, row["item_id"])
    assert seen.category == "screenshot"
    assert "CasaFranco 5GHz" in seen.visible_text
    assert "CasaFranco" not in seen.caption
    assert row["category"] == "photos", "a screenshot is still filed as a screenshot"


def test_the_description_makes_the_file_searchable(tmp_path: Path, monkeypatch) -> None:
    conn, settings = _scanned(tmp_path, name="IMG_9001.png")
    _answered(
        monkeypatch,
        {
            "caption": "A phone's Wi-Fi settings screen.",
            "tags": ["wifi", "settings"],
            "visible_text": "CasaFranco 5GHz",
        },
    )
    analyze_items(conn, settings)
    from librairy.search import sync_search_item

    item_id = conn.execute("SELECT id FROM items").fetchone()[0]
    sync_search_item(conn, item_id)

    for term in ("wifi", "CasaFranco", "settings"):
        found = conn.execute(
            "SELECT COUNT(*) FROM search_fts WHERE search_fts MATCH ?", (term,)
        ).fetchone()[0]
        assert found == 1, f"searching {term!r} should find the screenshot"


def test_an_unchanged_file_is_not_looked_at_twice(tmp_path: Path, monkeypatch) -> None:
    """A second pass of inference on the same picture buys nothing."""
    conn, settings = _scanned(tmp_path)
    calls: list = []
    _answered(monkeypatch, FULL_ANSWER, calls=calls)
    analyze_items(conn, settings)
    assert len(calls) == 1

    analyze_items(conn, settings, reanalyze=True)

    assert len(calls) == 1, "the stored description was reused"
    row = conn.execute("SELECT * FROM proposals WHERE status != 'superseded'").fetchone()
    assert row["clean_name"] == "IMG_4821-baby-orange-cat.jpg", "and the name is stable"


def test_a_dead_provider_is_asked_twice_and_then_left_alone(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    for index in range(5):
        _png(settings.inbox_dir / f"IMG_{index}.png")
    conn = connect(settings)
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    calls: list = []
    _answered(monkeypatch, None, calls=calls)

    analyze_items(conn, settings)

    assert len(calls) == 2, "one timeout per image on a dead endpoint is most of a day"


def test_no_local_provider_means_no_attempt(tmp_path: Path, monkeypatch) -> None:
    conn, settings = _scanned(tmp_path, LMSTUDIO_HOST="")
    _answered(monkeypatch, FULL_ANSWER, calls=(calls := []))

    analyze_items(conn, settings)

    assert calls == []


def test_a_disc_is_never_looked_at_or_renamed(tmp_path: Path, monkeypatch) -> None:
    """A .VOB is not an image, and a disc is answered before any enrichment.

    The names inside a VIDEO_TS are a contract with a player. This test exists
    because "add a feature that renames files" and "one folder whose filenames
    must never change" are one release away from meeting.
    """
    settings = _settings(tmp_path)
    disc = settings.inbox_dir / "Some Film (1999)" / "VIDEO_TS"
    disc.mkdir(parents=True)
    (disc / "VTS_01_1.VOB").write_bytes(b"x" * 32)
    # A disc really does carry a JPEG sometimes; it must not be described
    # either, because it is part of the disc.
    _png(disc / "VIDEO_TS.jpg")
    conn = connect(settings)
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    _answered(monkeypatch, FULL_ANSWER, calls=(calls := []))

    analyze_items(conn, settings)

    names = {
        row["clean_name"]
        for row in conn.execute("SELECT clean_name FROM proposals")
    }
    assert "VIDEO_TS/VTS_01_1.VOB" in names
    assert calls == [], "nothing inside a disc is sent to a model"


def test_the_migration_leaves_existing_rows_alone(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    conn = connect(settings)
    conn.execute(
        """
        INSERT INTO items(id, root, relpath, size, mtime_ns, fingerprint,
                          first_seen_at, last_seen_at)
        VALUES (1, 'inbox', 'a.jpg', 1, 1, 'fp1', 'now', 'now')
        """
    )
    conn.commit()
    conn.close()

    reopened = connect(settings)

    assert reopened.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
    columns = {row[1] for row in reopened.execute("PRAGMA table_info(vision_results)")}
    assert {"item_id", "fingerprint", "caption", "visible_text", "name_tokens"} <= columns
