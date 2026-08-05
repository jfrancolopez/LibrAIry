from __future__ import annotations

import json

import pytest

from librairy.ai.prompt import render_prompt, validate_ai_response
from librairy.ai.redact import build_view
from librairy.models import EvidenceEntry, Item
from librairy.taxonomy import render_destination


def item() -> Item:
    return Item(
        id=1,
        root="inbox",
        relpath="Paris #trip/report.pdf",
        size=1000,
        mtime_ns=1,
        fingerprint=None,
        state="discovered",
        first_seen_at="2026-01-01T00:00:00Z",
        last_seen_at="2026-01-01T00:00:00Z",
        missing_since=None,
    )


def valid_payload(**overrides) -> dict:
    payload = {
        "category": "documents",
        "name_fields": {"title": "Report", "year": 2026},
        "group_hint": None,
        "confidence": 0.9,
        "rationale": "document filename",
    }
    payload.update(overrides)
    return payload


def test_valid_fenced_response_becomes_answer() -> None:
    text = f"```json\n{json.dumps(valid_payload())}\n```"

    result = validate_ai_response(text)

    assert result.answer is not None
    assert result.answer.category == "documents"
    assert result.reason is None


@pytest.mark.parametrize(
    "payload,reason",
    [
        ({"category": "unknown"}, "literal_error"),
        ({"category": "documents", "name_fields": {}, "confidence": 0.9}, "missing"),
        (valid_payload(name_fields={"title": "bad/path"}), "value_error"),
        (valid_payload(confidence=2.0), "less_than_equal"),
        (valid_payload(extra="nope"), "extra_forbidden"),
    ],
)
def test_malformed_responses_are_discarded_with_reason(payload: dict, reason: str) -> None:
    result = validate_ai_response(json.dumps(payload))

    assert result.answer is None
    assert result.reason == reason


def test_prompt_snapshot_uses_only_redacted_view() -> None:
    metadata = {
        "tags": {"title": "/data/inbox/Paris/secret.pdf", "artist": "47.620500"},
        "city": "Paris",
        "gps_latitude": "47.620500",
    }
    view = build_view(
        item(), metadata, (EvidenceEntry("heuristic", "category", "/data/Paris", 0.2),)
    )

    prompt = render_prompt(view)

    assert "Redacted item view" in prompt
    assert "/data/" not in prompt
    assert "47.620500" not in prompt


@pytest.mark.parametrize(
    "category,fields",
    [
        ("music", {"artist": "A", "album": "B", "title": "C", "year": 2026}),
        ("movies", {"title": "Movie", "year": 2026}),
        ("shows", {"show": "Show", "season": 1, "episode": 2}),
        ("photos", {"event": "Trip", "year": 2026}),
        ("documents", {"title": "Report", "year": 2026}),
        ("books", {"author": "Author", "title": "Book"}),
        ("projects", {"project": "Project"}),
        ("misc", {"title": "Loose"}),
    ],
)
def test_validated_answer_fields_render_inside_library(
    tmp_path, category: str, fields: dict
) -> None:
    result = validate_ai_response(json.dumps(valid_payload(category=category, name_fields=fields)))

    assert result.answer is not None
    render_fields = dict(result.answer.name_fields)
    render_fields.setdefault(
        "clean_name", render_fields.get("title") or render_fields.get("project") or "Item"
    )
    render_fields.setdefault("genre", "General")
    render_fields.setdefault("track", 0)
    rendered = render_destination(category, render_fields, library_root=tmp_path / "library")

    assert rendered.relpath is not None


def test_json_is_found_inside_surrounding_prose() -> None:
    """Local models are far less disciplined than hosted ones about replying
    with nothing but JSON, and a stray sentence threw the whole answer away."""
    result = validate_ai_response(
        'Sure, here you go:\n{"category": "music", "confidence": 0.9, "rationale": "flac"}\n'
        "Let me know if you need anything else."
    )

    assert result.answer is not None
    assert result.answer.category == "music"


def test_nested_objects_are_not_cut_short() -> None:
    result = validate_ai_response(
        'note: {"category": "music", "name_fields": {"artist": "Queen"}, '
        '"confidence": 0.9, "rationale": "tags"} done'
    )

    assert result.answer is not None
    assert result.answer.name_fields["artist"] == "Queen"


def test_braces_inside_strings_do_not_end_the_object() -> None:
    result = validate_ai_response(
        '{"category": "misc", "confidence": 0.5, "rationale": "name has a } in it"}'
    )

    assert result.answer is not None
    assert "}" in result.answer.rationale


def test_prose_with_no_json_at_all_is_still_rejected() -> None:
    result = validate_ai_response("I am not able to classify this file.")

    assert result.answer is None
    assert result.reason == "invalid-json"


def test_an_unterminated_object_is_rejected() -> None:
    result = validate_ai_response('{"category": "music", "confidence": 0.9')

    assert result.answer is None


def test_every_provider_actually_sends_the_instructions() -> None:
    """SYSTEM_PROMPT existed, was tested, and no provider ever sent it.

    Each of ollama.py, cloud.py and lmstudio.py had its own _prompt_text that
    returned the bare redacted view, so every model was handed an anonymous
    JSON blob with no task attached. The local ones echoed it straight back.
    """
    import inspect

    from librairy.ai import cloud, lmstudio, ollama

    for module in (ollama, cloud, lmstudio):
        source = inspect.getsource(module)
        assert "prompt_for(view)" in source, f"{module.__name__} builds its own prompt"
        assert "def _prompt_text" not in source, f"{module.__name__} still has a private copy"


def test_prompt_carries_the_task_and_an_example_of_the_answer() -> None:
    from librairy.ai.prompt import prompt_for

    text = prompt_for({"file_name": "unknown.mp3"})

    assert "You classify files for LibrAIry." in text
    # A prose schema is enough for hosted models and not for a 4B local one;
    # without a worked example small models mirror the input structure back.
    assert '"category": "music"' in text
    assert "never repeat the input fields back" in text
    assert "unknown.mp3" in text
