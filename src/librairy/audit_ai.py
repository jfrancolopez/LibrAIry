"""The last tier, and deliberately the narrowest.

Everything above this module answers from something checkable: a byte-for-byte
hash, an embedded tag, a release id from a catalog. What reaches here is the
residue — the cases where the deterministic tiers disagreed with each other or
had nothing to say, and a catalog did not settle it either.

The rule is that a model is asked *last and least*. A file a hash explained, a
tag identified or MusicBrainz matched is not ambiguous, and sending it to a
language model would be paying for an answer already in hand — slowly, and
with a worse error mode. So the candidate list is built by subtraction, and
when it comes out empty the stage does not run at all. A progress panel that
says `Using AI` while calling nothing is worse than one that skips the line.

What is genuinely unresolved today is the **custom compilation**: a folder
whose tracks describe one coherent release that no configured catalog has
heard of. `audit_compilation` correctly refuses to call that either an
official release or a random pile, and that is exactly the judgement a model
can contribute to — it has read the sleeve notes of the world, and "Best Road
Trip Disco Fever Classics" either reads like a real published compilation or
it reads like something somebody made for a drive.

What the model contributes is **evidence, not a verdict**. Its answer becomes
one more line in Why, alongside the tags and the catalogs, and it cannot
promote a collection to `recognized`; only a catalog id does that. A model
that is confident and wrong is the failure mode this whole design is built
around, and giving it the deciding vote on whether forty-five files stay
together would undo the point.

A provider being down is not an audit failing. The count of candidates that
went unreviewed is recorded and reported, because "3 ambiguous items not
AI-reviewed" is a successful audit saying which part of itself was missing.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from librairy.ai.base import HealthResult
from librairy.ai.redact import RedactedItemView, _safe_component
from librairy.ai.status import upsert_provider_status
from librairy.models import EvidenceEntry

if TYPE_CHECKING:  # pragma: no cover - typing only
    from librairy.audit import Finding
    from librairy.audit_stages import Context

LOGGER = logging.getLogger(__name__)

# The kinds this tier is allowed to think about. Anything not listed was
# settled by a cheaper tier, and re-asking is how an "AI only for ambiguity"
# design quietly becomes "AI for everything".
UNRESOLVED_KINDS = frozenset({"collection-custom"})

# A whole-library audit must not turn into a long conversation with a model.
# Twenty is more unresolved collections than a real library has, and a library
# that does have more has a bigger problem than this stage can help with.
MAX_CANDIDATES = 20


@dataclass(frozen=True)
class Candidate:
    """One unresolved question, and the redacted description that asks it."""

    finding: Finding
    view: RedactedItemView


def candidates(context: Context) -> list[Candidate]:
    """The findings nothing above could settle. Built by subtraction."""
    found: list[Candidate] = []
    for finding in context.findings:
        if finding.kind not in UNRESOLVED_KINDS:
            continue
        found.append(Candidate(finding, _view_for(finding)))
        if len(found) >= MAX_CANDIDATES:
            break
    return found


def _view_for(finding: Finding) -> RedactedItemView:
    """The collection described the way every other AI call describes a file.

    Reusing `RedactedItemView` rather than inventing a second prompt shape is
    what keeps redaction honest: there is one place that decides what leaves
    this machine, and a new one would be a new place to forget.
    """
    from pathlib import PurePosixPath

    path = PurePosixPath(finding.relpath)
    return RedactedItemView(
        display_path=_safe_component(path.name),
        file_name=_safe_component(path.name),
        extension="",
        size_bucket="unknown",
        media_kind="music",
        embedded_album=_tag(finding, "album"),
        folder_chain=tuple(_safe_component(part) for part in path.parts[:-1]),
        evidence_summaries=tuple(
            f"{entry.source}:{entry.field}:{entry.detail}:{entry.weight:.2f}"
            for entry in finding.evidence
            if entry.field in {"collection", "album", "tracks", "artists", "agreement"}
        )[:20],
    )


def _tag(finding: Finding, field: str) -> str | None:
    for entry in finding.evidence:
        if entry.source == "tags" and entry.field == field:
            return entry.detail
    return None


def review(context: Context, candidate: Candidate) -> bool:
    """Ask the configured providers about one candidate. True if any answered.

    Status is recorded the way the classification path records it — a real
    answer sets `last_used_at`, a failure sets `last_error` and neither — so
    the settings header's "answered X ago" keeps meaning an actual inference.
    A health check must never be able to write that field, which is the bug
    this whole area was fixed for.
    """
    from librairy.ai.orchestrator import provider_for_config
    from librairy.ai.registry import provider_chain

    try:
        configs = provider_chain(context.conn, context.settings, record=False)
    except Exception:  # noqa: BLE001 - an unreadable registry is no AI, not a crash
        LOGGER.warning("AI provider chain unavailable", exc_info=True)
        return False

    for config in configs:
        try:
            provider = provider_for_config(config, context.settings)
        except ValueError:  # pragma: no cover - unknown kind in the registry
            continue
        started = time.monotonic()
        try:
            answer = provider.classify(candidate.view, context.settings.ai_timeout)
        except (OSError, RuntimeError) as exc:
            upsert_provider_status(
                context.conn, config, HealthResult(False, error=exc.__class__.__name__)
            )
            continue
        if answer is None:
            continue
        upsert_provider_status(
            context.conn,
            config,
            HealthResult(True, latency_ms=max(0, round((time.monotonic() - started) * 1000))),
            used=True,
        )
        candidate.finding.evidence.append(
            EvidenceEntry("ai", "reading", _reading(config.name, answer), answer.confidence)
        )
        return True
    return False


def _reading(provider: str, answer) -> str:
    """One sentence, attributed, and worded so nobody mistakes it for proof."""
    named = str(answer.name_fields.get("artist") or "").strip()
    who = f" It reads the release artist as {named!r}." if named else ""
    return f"{provider} suggests: {answer.rationale.strip()}{who}"
