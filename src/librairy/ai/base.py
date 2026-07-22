from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from librairy.ai.redact import RedactedItemView
from librairy.models import Category


class AIAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: Category
    name_fields: dict[str, str | int] = Field(default_factory=dict)
    group_hint: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str

    @field_validator("name_fields")
    @classmethod
    def validate_name_fields(cls, value: dict[str, str | int]) -> dict[str, str | int]:
        allowed = {
            "artist",
            "album",
            "title",
            "show",
            "season",
            "episode",
            "year",
            "event",
            "project",
            "author",
        }
        invalid = sorted(set(value) - allowed)
        if invalid:
            raise ValueError(f"unsupported name fields: {', '.join(invalid)}")
        for field, field_value in value.items():
            if isinstance(field_value, int):
                continue
            if len(field_value) > 120:
                raise ValueError(f"name field too long: {field}")
            if "/" in field_value or "\\" in field_value:
                raise ValueError(f"path separator in name field: {field}")
        return value


@dataclass(frozen=True)
class HealthResult:
    ok: bool
    latency_ms: int | None = None
    error: str | None = None
    models: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    kind: str
    endpoint: str | None
    model: str
    enabled: bool
    is_local: bool


class Provider(Protocol):
    config: ProviderConfig

    def health(self, timeout: int) -> HealthResult: ...

    def classify(self, view: RedactedItemView, timeout: int) -> AIAnswer | None: ...
