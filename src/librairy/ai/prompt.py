from __future__ import annotations

import json
import re
from dataclasses import dataclass

from pydantic import ValidationError

from librairy.ai.base import AIAnswer
from librairy.ai.redact import RedactedItemView

# Describing the schema in prose is enough for the hosted models and not for a
# 4B local one: given a JSON input and no example, small models mirror the
# input structure straight back. Showing the exact shape of the answer, and
# saying outright not to repeat the input, is what makes local-first work.
SYSTEM_PROMPT = """You classify files for LibrAIry.

Given the description of one file, decide which category it belongs to and
what it should be called. Reply with a single JSON object and nothing else.

Categories: music, movies, shows, photos, documents, books, projects, misc.

Reply in exactly this shape:
{"category": "music",
 "name_fields": {"artist": "Queen", "album": "A Night at the Opera",
                 "title": "Bohemian Rhapsody", "year": 1975},
 "group_hint": "album:Queen:A Night at the Opera",
 "confidence": 0.82,
 "rationale": "audio file, artist and album in the folder name"}

Rules:
- category must be one of the eight listed above.
- name_fields keys may only be: artist, album, title, show, season, episode,
  year, event, project, author. Leave out any you do not know.
- confidence is a number between 0 and 1. Be honest — use a low value when guessing.
- rationale is one short sentence saying what you went on.
- Never include a file path, and never repeat the input fields back.
"""

FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S | re.I)


@dataclass(frozen=True)
class ValidationResult:
    answer: AIAnswer | None
    reason: str | None = None


def render_prompt(view: RedactedItemView) -> str:
    return prompt_for(view)


def prompt_for(view: object) -> str:
    """Instructions plus the redacted view — what every provider must send.

    This lived as a private `_prompt_text` copied into ollama.py, cloud.py and
    lmstudio.py, and all three copies returned the bare view JSON without the
    instructions. Every provider was handing the model an anonymous blob and
    hoping; the local models responded by echoing the input straight back.
    One copy now, so there is one place for that to be true or false.
    """
    body = (
        view.model_dump_json()
        if hasattr(view, "model_dump_json")
        else json.dumps(view, sort_keys=True)
    )
    return f"{SYSTEM_PROMPT}\nRedacted item view:\n{body}"


def validate_ai_response(text: str) -> ValidationResult:
    payload = extract_json(text)
    if payload is None:
        return ValidationResult(None, "invalid-json")
    try:
        return ValidationResult(AIAnswer.model_validate(payload))
    except ValidationError as exc:
        return ValidationResult(None, exc.errors()[0]["type"])


def extract_json(text: str) -> dict | None:
    """The one JSON object in a model's reply, however it chose to wrap it.

    Shared with the image path, which faces exactly the same problem: a local
    model asked for JSON returns JSON inside a code fence, after an apology, or
    with a note underneath.
    """
    match = FENCE_RE.search(text.strip())
    candidate = match.group(1) if match else text
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        payload = _first_json_object(candidate)
    return payload if isinstance(payload, dict) else None


def _first_json_object(text: str) -> dict | None:
    """The first complete `{...}` in a reply that also contains prose.

    Local models are far less disciplined than the hosted ones about answering
    with nothing but JSON — a stray "Sure, here you go:" or a trailing note
    used to throw the whole classification away.
    """
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : index + 1])
                except json.JSONDecodeError:
                    return None
    return None
