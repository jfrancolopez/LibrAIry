"""What LibrAIry knows about one finding, arranged so a person can judge it.

A Review row has to be scannable, so it says one sentence and three numbers.
But behind `Best Road Trip Disco Fever Classics` there are twenty checkable
facts — every track agrees on the album, on the barcode, on the year; the
numbering runs 1 to 45 with no gaps; two catalogs were asked and neither had
heard of it — and none of that was reachable. The row asked you to trust a
verdict while holding the reasons for it out of sight.

So the row stays compact and this builds the panel underneath it. Three ideas
carry the whole design:

**Facts, checks and interpretation are kept apart.** "45 tracks" is measured.
"MusicBrainz found nothing" is the result of asking someone else. "This is a
custom compilation" is LibrAIry's opinion. Running them together as one
paragraph is how a conclusion comes to look like an observation, and the
conclusion is the only part that could be wrong.

**Agreement counts, not assertions.** `Album: Best Road Trip…` is a claim.
`45 of 45 tracks agree` is a reason, and it is also how you notice the case
where forty-four agree and one does not.

**No invented confidence.** There is no aggregate score here and there is not
going to be one, because the audit has no model that would make a percentage
mean anything. `7 signals agree · 0 contradictions · 2 catalogs checked · 0
matches` is four numbers a person can check, which is worth more than one
number nobody can.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

# What a catalog row can say. A blank was genuinely ambiguous: "we asked and
# there was nothing" and "we never asked" are different claims about the
# world, and the old UI rendered both as an absent line.
STATUS_LABEL = {
    "matched": "Matched",
    "no-match": "Checked — no match",
    "not-checked": "Not checked",
    "unavailable": "Unavailable",
    "not-applicable": "Not applicable",
    "cached": "Cached",
}
# Sources that are things LibrAIry measured itself.
FACT_SOURCES = frozenset({"filesystem", "tags", "fingerprint", "exif", "content"})
# Sources that are somebody else's answer.
CATALOG_SOURCES = frozenset(
    {"musicbrainz", "discogs", "tmdb", "tvmaze", "openlibrary", "lastfm", "acoustid"}
)
# `.title()` gets these wrong in ways their owners would notice: MusicBrainz,
# not Musicbrainz; TMDB, not Tmdb.
CATALOG_LABEL = {
    "musicbrainz": "MusicBrainz",
    "discogs": "Discogs",
    "tmdb": "TMDB",
    "tvmaze": "TVmaze",
    "openlibrary": "Open Library",
    "lastfm": "Last.fm",
    "acoustid": "AcoustID",
}

# Rows whose value is a raw number that reads better formatted, and what to
# call them. The evidence field names are terse because they are also read by
# the detectors; the panel is read by a person.
FACT_LABELS = {
    "tracks": "Tracks",
    "artists": "Artists",
    "folders": "Folders",
    "total bytes": "Total size",
    "each": "Size of each copy",
    "album": "Album",
    "artist": "Artist",
    "album artist": "Album artist",
    "track numbers": "Track sequence",
    "blake2b": "Fingerprint",
}
# Fields that are plumbing rather than evidence a reader wants in a table.
HIDDEN_FIELDS = frozenset({"folder", "also at", "agreement", "disagreement", "move"})


@dataclass(frozen=True)
class DetailRow:
    label: str
    value: str
    note: str = ""
    status: str = ""

    @property
    def status_label(self) -> str:
        return STATUS_LABEL.get(self.status, "")

    @property
    def is_conflict(self) -> bool:
        return self.status == "conflict"


@dataclass
class DetailSection:
    title: str
    kind: str
    rows: list[DetailRow] = field(default_factory=list)
    note: str = ""

    def __bool__(self) -> bool:
        return bool(self.rows)


@dataclass
class Details:
    """The whole panel. Every part optional; empty ones are not rendered."""

    summary: list[tuple[str, str]] = field(default_factory=list)
    sections: list[DetailSection] = field(default_factory=list)
    conflicts: list[DetailRow] = field(default_factory=list)
    verdict: str = ""
    explanation: list[str] = field(default_factory=list)
    reservations: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.sections or self.summary)


def build(row: sqlite3.Row, entries: list, *, size_label: str = "") -> Details:
    """Arrange one finding's evidence into the panel.

    Reads only what the detector already recorded. Nothing is recomputed, no
    file is opened and no catalog is asked — opening a details panel is a
    render, and a render that could touch the network would make Review as
    slow as an audit.
    """
    facts: list[DetailRow] = []
    conflicts: list[DetailRow] = []
    checks: list[DetailRow] = []
    ai: list[DetailRow] = []
    verdict = ""
    agree_count = 0

    for entry in entries:
        if entry.field in HIDDEN_FIELDS:
            # Counted, not shown: the sentence form of the same evidence is
            # what Why renders, and repeating it here would double every fact.
            agree_count += entry.field == "agreement"
            continue
        if entry.source == "ai":
            ai.append(DetailRow("AI reading", entry.detail, entry.note))
            continue
        if entry.source in CATALOG_SOURCES:
            checks.append(
                DetailRow(
                    CATALOG_LABEL.get(entry.source, entry.source.title()),
                    entry.detail,
                    entry.note,
                    entry.status,
                )
            )
            continue
        if entry.field == "collection":
            verdict = entry.detail
            continue
        row_out = _fact_row(entry)
        (conflicts if entry.status == "conflict" else facts).append(row_out)

    # The same fact can arrive twice: once as a plain entry a detector wrote
    # for Why, once as a `fact:` row carrying its agreement count. Keep the
    # one that says how many tracks agreed — a row without that number is the
    # weaker version of the same statement, not a second statement.
    facts = _dedupe(facts)
    if size_label and not any(item.label == "Total size" for item in facts):
        facts.append(DetailRow("Size", size_label))

    details = Details(verdict=verdict, conflicts=conflicts)
    details.summary = _summary(agree_count, conflicts, checks)
    for title, kind, rows in (
        ("Facts", "facts", facts),
        ("Conflicts", "conflicts", conflicts),
        ("Catalog checks", "checks", checks),
        ("AI review", "ai", ai),
    ):
        if rows:
            details.sections.append(
                DetailSection(title, kind, rows, note=_section_note(kind))
            )
    return details


def _dedupe(rows: list[DetailRow]) -> list[DetailRow]:
    """One row per label, keeping whichever carries an agreement count."""
    best: dict[str, DetailRow] = {}
    for row in rows:
        existing = best.get(row.label)
        if existing is None or (row.note and not existing.note):
            best[row.label] = row
    return list(best.values())


def _section_note(kind: str) -> str:
    if kind == "facts":
        return "Read from the files themselves."
    if kind == "checks":
        return "Answers from outside this machine."
    if kind == "ai":
        return "Supporting evidence only. It does not establish a release identity."
    return ""


def _fact_row(entry) -> DetailRow:  # noqa: ANN001
    field_name = entry.field
    if field_name.startswith("fact:"):
        label, value = field_name[5:], entry.detail
    else:
        label = FACT_LABELS.get(field_name, field_name.replace("-", " ").capitalize())
        value = _value(field_name, entry.detail)
    return DetailRow(label, value, entry.note, entry.status)


def _value(field_name: str, detail: str) -> str:
    """Bytes as a size, everything else as written."""
    if field_name in {"total bytes", "each"}:
        from librairy.web.review import human_size

        return human_size(detail) or detail
    return detail


def _summary(agreements: int, conflicts: list[DetailRow], checks: list[DetailRow]) -> list:
    """Four numbers instead of one percentage.

    The temptation is an aggregate score, and there is no honest way to
    compute one: the audit has no model that says what a barcode is worth
    against a track sequence. These four are each checkable by hand, which is
    the property a progress number needs and a confidence number never had.
    """
    asked = [row for row in checks if row.status in {"matched", "no-match"}]
    matched = [row for row in checks if row.status == "matched"]
    # A lone "0 contradictions" on a finding that has nothing to weigh reads
    # as a verdict on evidence that was never gathered. The summary is for
    # findings where something was actually weighed.
    if not agreements and not checks and not conflicts:
        return []
    summary = []
    if agreements:
        summary.append((f"{agreements} signal{_s(agreements)} agree", "agree"))
    summary.append((f"{len(conflicts)} contradiction{_s(len(conflicts))}", "conflict"))
    if checks:
        plural = "es" if len(matched) != 1 else ""
        summary.append((f"{len(asked)} catalog{_s(len(asked))} checked", "check"))
        summary.append((f"{len(matched)} catalog match{plural}", "check"))
    return summary


def _s(count: int) -> str:
    return "" if count == 1 else "s"


# --- the three decisions -------------------------------------------------------
#
# `Keep together` and `No change` are the pair most easily confused, and they
# are opposites. Keeping the compilation together *fixes* the twenty-seven
# folder mess by consolidating it. No change *leaves* the twenty-seven folders
# exactly as they are and stops asking about them. One is a correction and one
# is a decision that there is nothing to correct.

DECISIONS = {
    "collection-recognized": ("keep", "no-change"),
    "collection-custom": ("keep", "split", "no-change"),
    "collection-loose": ("split", "keep", "no-change"),
}

DECISION_TEXT = {
    "keep": (
        "Keep together",
        "Treat this as one release and put it in a single folder. "
        "The tracks stop being spread across artist folders.",
    ),
    "split": (
        "Organize individually",
        "Treat the collection folder as filing rather than identity, and place "
        "each track using its own artist and album.",
    ),
    "no-change": (
        "No change",
        "Leave the current layout exactly as it is, and stop reporting this. "
        "Nothing moves.",
    ),
}


@dataclass(frozen=True)
class Decision:
    key: str
    label: str
    meaning: str
    recommended: bool = False
    destination: str = ""


def decisions(kind: str, destination: str = "") -> list[Decision]:
    """What can be chosen here, recommendation first, each saying what it means.

    Ordered by what the evidence supports rather than alphabetically, and the
    recommendation is marked rather than merely first — a reader who scans
    should not have to infer it from position.
    """
    keys = DECISIONS.get(kind, ())
    return [
        Decision(
            key=key,
            label=DECISION_TEXT[key][0],
            meaning=DECISION_TEXT[key][1],
            recommended=index == 0,
            destination=destination if key == "keep" else "",
        )
        for index, key in enumerate(keys)
    ]


RECOMMENDATION_WHY = {
    "collection-recognized": (
        "A catalog identifies this as one release, so the twenty-seven folders "
        "are filing rather than identity."
    ),
    "collection-custom": (
        "The files describe one coherent compilation even though no configured "
        "catalog recognises it."
    ),
    "collection-loose": (
        "No reliable shared release identity was found. Each track has stronger "
        "individual artist and album evidence than the folder does."
    ),
}


def recommendation(kind: str, destination: str) -> dict | None:
    chosen = decisions(kind, destination)
    if not chosen:
        return None
    first = chosen[0]
    return {
        "label": first.label,
        "meaning": first.meaning,
        "destination": first.destination,
        "why": RECOMMENDATION_WHY.get(kind, ""),
        "alternatives": chosen[1:],
    }


# --- what the change would look like -------------------------------------------

PREVIEW_LIMIT = 3


def current_shape(row: sqlite3.Row, folders: list[str]) -> list[str]:
    """The problem, described rather than counted.

    "Spans 27 folders" states a number. "The same compilation folder is
    repeated underneath each artist" states what is wrong with it, which is
    what a reader needs in order to agree or disagree.
    """
    if len(folders) < 2:
        return []
    names = [folder.rsplit("/", 1)[0] for folder in folders]
    shown = [f"{name}/" for name in names[:PREVIEW_LIMIT]]
    if len(names) > PREVIEW_LIMIT:
        shown.append(f"+{len(names) - PREVIEW_LIMIT} more artist folders")
    return shown


def current_shape_note(kind: str, folders: list[str]) -> str:
    """One sentence saying what is wrong with the shape above.

    A list of folders is a fact. "The same folder name is repeated underneath
    every artist" is the observation that makes the fact worth reading.
    """
    if len(folders) < 2 or not kind.startswith("collection-"):
        return ""
    leaf = folders[0].rsplit("/", 1)[-1]
    return (
        f"The folder {leaf!r} is repeated underneath each of "
        f"{len(folders)} artists, so the release is not together anywhere."
    )


def proposed(pairs: list[tuple[str, str]], limit: int = PREVIEW_LIMIT) -> dict:
    """Where each file would go, a few at a time.

    Forty-five paths in a Review row is not a preview, it is the row becoming
    a report. The first few plus a count is enough to see the shape, and the
    rest are one click away.
    """
    if not pairs:
        return {}
    shown = [
        {"from": source, "to": destination} for source, destination in pairs[:limit]
    ]
    folders = {destination.rsplit("/", 1)[0] for _, destination in pairs}
    return {
        "count": len(pairs),
        "shown": shown,
        "rest": [
            {"from": source, "to": destination} for source, destination in pairs[limit:]
        ],
        "more": max(0, len(pairs) - limit),
        "summary": f"{len(pairs)} files to {len(folders)} folders",
    }
