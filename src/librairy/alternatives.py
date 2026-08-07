"""The other answers, gathered on demand for one file.

Analysis keeps the winner and throws the rest away. That is right for a scan —
storing five candidates for every file in a fifty-thousand-file library is a
lot of rows nobody will ever read — but it leaves Review with one guess and no
way to see what else was on the table. When the guess is wrong, "here is
another one you can have instead" is the fastest possible correction.

So the alternatives are fetched when you ask for them, about the one file you
are looking at, and are never stored. Fresh every time, which also means a
catalog key or an AI provider added five minutes ago is included without
re-analysing anything.

Applying an option deliberately goes through the ordinary edit path, so a
choice made here is validated, contained and journalled exactly like a
destination typed by hand.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from librairy.ai.orchestrator import every_provider_answer
from librairy.config import Settings
from librairy.models import Item
from librairy.taxonomy import render_destination


@dataclass(frozen=True)
class Option:
    """One answer somebody gave, in a form Review can offer as a choice."""

    source: str
    source_label: str
    kind: str
    title: str
    detail: str
    category: str
    clean_name: str
    dest_relpath: str | None
    confidence: float
    current: bool = False

    @property
    def confidence_pct(self) -> int:
        return round(self.confidence * 100)

    @property
    def band(self) -> str:
        """Its own band, not the row's. An 85% option inside a 30% row was
        drawing its score in the row's red — the one number on screen that is
        supposed to say "this one is better" said the opposite."""
        if not self.dest_relpath or self.confidence < 0.6:
            return "low"
        return "high" if self.confidence >= 0.85 else "mid"


@dataclass(frozen=True)
class OptionSet:
    proposal_id: int
    item_id: int
    relpath: str
    options: list[Option]
    asked: list[str]
    problems: list[str]

    @property
    def alternatives(self) -> list[Option]:
        return [option for option in self.options if not option.current]

    @property
    def summary(self) -> str:
        if not self.asked:
            return (
                "Nothing to ask. Switch on an AI provider in Settings → AI, or a catalog "
                "in Settings → Catalogs, and this can offer you something."
            )
        count = len(self.alternatives)
        if not count:
            return f"Asked {_join(self.asked)}. Nothing came back with a different answer."
        return f"Asked {_join(self.asked)}. {count} other answer{'s' if count != 1 else ''}."


def options_for_proposal(
    conn: sqlite3.Connection, settings: Settings, proposal_id: int
) -> OptionSet:
    row = conn.execute(
        """
        SELECT p.id, p.item_id, p.category, p.clean_name, p.dest_relpath, p.confidence,
               i.root, i.relpath, i.size, i.mtime_ns, i.fingerprint, i.state,
               i.first_seen_at, i.last_seen_at, i.missing_since
        FROM proposals p JOIN items i ON i.id = p.item_id
        WHERE p.id=?
        """,
        (proposal_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"proposal not found: {proposal_id}")
    item = Item(
        id=int(row["item_id"]),
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
    options = [_current_option(row)]
    asked: list[str] = []
    problems: list[str] = []
    ai_options, ai_asked, ai_problems = _ai_options(conn, settings, item, row)
    options.extend(ai_options)
    asked.extend(ai_asked)
    problems.extend(ai_problems)
    return OptionSet(
        proposal_id=int(row["id"]),
        item_id=int(row["item_id"]),
        relpath=row["relpath"],
        options=_deduplicated(options),
        asked=asked,
        problems=problems,
    )


def _current_option(row: sqlite3.Row) -> Option:
    return Option(
        source="current",
        source_label="What LibrAIry chose",
        kind="current",
        title=row["clean_name"],
        detail="The guess on the row now.",
        category=row["category"],
        clean_name=row["clean_name"],
        dest_relpath=row["dest_relpath"],
        confidence=float(row["confidence"] or 0.0),
        current=True,
    )


@dataclass(frozen=True)
class _Current:
    """The minimum `every_provider_answer` needs of a classification."""

    confidence: float
    evidence: tuple = ()


def _ai_options(
    conn: sqlite3.Connection, settings: Settings, item: Item, row: sqlite3.Row
) -> tuple[list[Option], list[str], list[str]]:
    options: list[Option] = []
    asked: list[str] = []
    problems: list[str] = []
    for answer in every_provider_answer(
        conn, settings, item, _Current(float(row["confidence"] or 0.0))
    ):
        label = f"{answer.config.name} ({answer.config.model})"
        asked.append(label)
        if answer.classification is None:
            problems.append(f"{label}: {answer.problem or 'nothing to say'}")
            continue
        result = answer.classification
        options.append(
            Option(
                source=f"ai:{answer.config.name}",
                source_label=("Cloud AI" if not answer.config.is_local else "Local AI")
                + f" · {label}",
                kind="cloud" if not answer.config.is_local else "ai",
                title=result.clean_name,
                detail=_rationale(result),
                category=result.category,
                clean_name=result.clean_name,
                # A provider whose answer scored under the threshold has its
                # destination stripped during a scan, because nothing that
                # unsure should file itself. Choosing it by hand is a different
                # act, so the path is rendered again here — the option is only
                # useful if it says where the file would go.
                dest_relpath=result.dest_relpath or _rendered(settings, result),
                confidence=result.confidence,
            )
        )
    return options, asked, problems


def _rendered(settings: Settings, result) -> str | None:
    rendered = render_destination(result.category, result.fields, library_root=settings.library_dir)
    return rendered.relpath


def _rationale(result) -> str:
    for entry in reversed(tuple(result.evidence)):
        if entry.source == "ai":
            _, _, said = entry.detail.partition(": ")
            return said or entry.detail
    return ""


def _deduplicated(options: list[Option]) -> list[Option]:
    """Two providers agreeing is worth knowing once, not twice.

    Keyed on where the file would end up, since that is what the choice is
    actually about; the current guess always survives.
    """
    seen: set[str] = set()
    kept: list[Option] = []
    for option in options:
        key = f"{option.category}|{option.dest_relpath or option.clean_name}"
        if key in seen and not option.current:
            continue
        seen.add(key)
        kept.append(option)
    return kept


def _join(names: list[str]) -> str:
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + f" and {names[-1]}"
