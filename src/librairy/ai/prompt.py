from __future__ import annotations

import json
import re
from dataclasses import dataclass

from pydantic import ValidationError

from librairy.ai.base import AIAnswer
from librairy.ai.redact import RedactedItemView

SYSTEM_PROMPT = """You classify files for LibrAIry.
Return strict JSON only. Do not return paths.
Categories: music, movies, shows, photos, documents, books, projects, misc.
Schema: category, name_fields, group_hint, confidence, rationale.
name_fields may include artist, album, title, show, season, episode, year, event, project, author.
"""

FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S | re.I)


@dataclass(frozen=True)
class ValidationResult:
    answer: AIAnswer | None
    reason: str | None = None


def render_prompt(view: RedactedItemView) -> str:
    return f"{SYSTEM_PROMPT}\nRedacted item view:\n{view.model_dump_json()}"


def validate_ai_response(text: str) -> ValidationResult:
    payload = _extract_json(text)
    if payload is None:
        return ValidationResult(None, "invalid-json")
    try:
        return ValidationResult(AIAnswer.model_validate(payload))
    except ValidationError as exc:
        return ValidationResult(None, exc.errors()[0]["type"])


def _extract_json(text: str) -> dict | None:
    match = FENCE_RE.search(text.strip())
    candidate = match.group(1) if match else text
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None
