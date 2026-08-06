"""Query and form parameter types that survive an empty HTML field.

An optional number left blank in a form is not absent — the browser submits
``year=``. FastAPI rejects that for ``int | None`` with a 422, and htmx does
not swap error responses, so the page simply stops responding with nothing on
screen to say why. That is exactly how the Browse search box died: every
search carried ``year=&genre=`` from the untouched filter inputs.

Coercing at the type is the only fix that stays fixed. A per-route ``if not
value`` is a thing to remember on the next route.
"""

from __future__ import annotations

from typing import Annotated, TypeVar

from pydantic import BeforeValidator

T = TypeVar("T")


def _blank_to_none(value: object) -> object:
    return None if isinstance(value, str) and not value.strip() else value


def _blank_to_one(value: object) -> object:
    """Page numbers: a blank one means the first page, never a 422."""
    return 1 if isinstance(value, str) and not value.strip() else value


OptionalInt = Annotated[int | None, BeforeValidator(_blank_to_none)]
OptionalFloat = Annotated[float | None, BeforeValidator(_blank_to_none)]
PageNumber = Annotated[int, BeforeValidator(_blank_to_one)]
