"""Everything the detectors know about one pair of copies, in one place.

A duplicate arrives as a single sentence — "exact duplicate of
library:Music/…" — and that is not enough to decide anything with. Three
separate detectors already ran (BLAKE2b fingerprints, rmlint, czkawka), each
answering a different question, and their answers were thrown away as soon as
one of them produced a verdict.

This keeps them. For every candidate pair it records what each detector
concluded and why, and then asks ffprobe or exiftool about both copies so the
differences that actually decide the question — bitrate, resolution, duration,
which camera, which is newer — are on the page next to the two previews.

Nothing here touches the filesystem beyond reading. The report is advice; the
proposal it hangs off is still the thing you approve or reject.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from librairy.config import Settings
from librairy.humanize import human_bytes
from librairy.mediakind import kind_for
from librairy.models import Item
from librairy.planner import utc_now

#  A detector's answer about the pair.
SAME = "same"
DIFFERENT = "different"
SIMILAR = "similar"
NOT_ASKED = "not-asked"
UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ToolFinding:
    """What one detector concluded, in its own words."""

    tool: str
    label: str
    verdict: str
    headline: str
    detail: str = ""


@dataclass(frozen=True)
class FactRow:
    """One property, measured on both copies."""

    label: str
    inbox: str
    library: str

    @property
    def same(self) -> bool:
        return self.inbox == self.library


@dataclass(frozen=True)
class DuplicateReport:
    item_id: int
    other_id: int
    #  "identical" | "same-content" | "similar" | "unclear"
    verdict: str
    summary: str
    recommendation: str
    findings: tuple[ToolFinding, ...] = ()
    facts: tuple[FactRow, ...] = ()
    checked_at: str = ""

    @property
    def differences(self) -> tuple[FactRow, ...]:
        return tuple(fact for fact in self.facts if not fact.same)


def compare(
    conn: sqlite3.Connection,
    settings: Settings,
    duplicate: Item,
    keeper: Item,
    *,
    rmlint: str = NOT_ASKED,
    kind_hint: str = "",
) -> DuplicateReport:
    """Build the report for one pair. `rmlint` is what the exact pass concluded."""
    left = _path_for(settings, duplicate)
    right = _path_for(settings, keeper)
    findings = [
        _fingerprint_finding(duplicate, keeper),
        _rmlint_finding(rmlint),
        _czkawka_finding(conn, duplicate, keeper),
        _size_finding(duplicate, keeper),
    ]
    facts = _facts(settings, duplicate, keeper, left, right)
    media = _media_finding(facts, kind_hint or kind_for(left))
    if media is not None:
        findings.append(media)
    verdict, summary, recommendation = _conclude(findings, facts, duplicate, keeper)
    return DuplicateReport(
        item_id=duplicate.id,
        other_id=keeper.id,
        verdict=verdict,
        summary=summary,
        recommendation=recommendation,
        findings=tuple(findings),
        facts=tuple(facts),
        checked_at=utc_now(),
    )


# --- the detectors --------------------------------------------------------


def _fingerprint_finding(duplicate: Item, keeper: Item) -> ToolFinding:
    label = "BLAKE2b fingerprint"
    if not duplicate.fingerprint or not keeper.fingerprint:
        return ToolFinding(
            "fingerprint",
            label,
            NOT_ASKED,
            "one copy has not been hashed yet",
            "Hashing happens on the scan after a file settles. Give it a cycle.",
        )
    if duplicate.fingerprint == keeper.fingerprint:
        return ToolFinding(
            "fingerprint",
            label,
            SAME,
            "byte-for-byte identical",
            f"Both hash to {duplicate.fingerprint[:16]}…. Every byte matches, so there is "
            f"nothing in one copy that is not in the other.",
        )
    return ToolFinding(
        "fingerprint",
        label,
        DIFFERENT,
        "not the same bytes",
        f"{duplicate.fingerprint[:16]}… against {keeper.fingerprint[:16]}…. They may still "
        f"be the same recording or photo — a different encode has different bytes.",
    )


def _rmlint_finding(verdict: str) -> ToolFinding:
    label = "rmlint"
    if verdict == SAME:
        return ToolFinding(
            "rmlint",
            label,
            SAME,
            "agrees, same file",
            "A second, independent implementation compared the two files and reached the "
            "same conclusion. Two tools agreeing is what makes this safe to act on.",
        )
    if verdict == DIFFERENT:
        return ToolFinding(
            "rmlint",
            label,
            DIFFERENT,
            "disagrees with the fingerprint",
            "The hashes match but rmlint did not pair these files. That is rare and worth "
            "a look before you move anything — nothing has been staged automatically.",
        )
    return ToolFinding(
        "rmlint",
        label,
        NOT_ASKED,
        "not asked",
        "Switched off in Settings → Library. Fingerprints alone decided this one.",
    )


def _czkawka_finding(conn: sqlite3.Connection, duplicate: Item, keeper: Item) -> ToolFinding:
    label = "czkawka"
    if not _option(conn, "dedup.use_czkawka", default=True):
        return ToolFinding(
            "czkawka",
            label,
            NOT_ASKED,
            "not asked",
            "Near-identical media detection is switched off in Settings → Library.",
        )
    if not _worker_flag(conn, "dedup.czkawka.available", default=True):
        return ToolFinding(
            "czkawka",
            label,
            UNAVAILABLE,
            "not installed",
            "The czkawka binary is missing from this container, so nothing was compared "
            "visually.",
        )
    row = conn.execute(
        """
        SELECT kind, score FROM similar_media_flags
        WHERE (item_id = ? AND similar_item_id = ?) OR (item_id = ? AND similar_item_id = ?)
        ORDER BY score DESC
        """,
        (duplicate.id, keeper.id, keeper.id, duplicate.id),
    ).fetchone()
    if row is None:
        return ToolFinding(
            "czkawka",
            label,
            NOT_ASKED,
            "nothing flagged",
            "It compares pictures by what they look like rather than by their bytes, and "
            "it did not pair these two.",
        )
    score = row["score"]
    measured = f" at a similarity of {score:.2f}" if isinstance(score, int | float) else ""
    return ToolFinding(
        "czkawka",
        label,
        SIMILAR,
        f"looks like the same {row['kind']}",
        f"Paired by appearance{measured}, which catches a re-encode or a resize that no "
        f"hash ever will.",
    )


def _size_finding(duplicate: Item, keeper: Item) -> ToolFinding:
    label = "file size"
    if duplicate.size == keeper.size:
        return ToolFinding(
            "size",
            label,
            SAME,
            "the same size",
            f"Both are {human_bytes(duplicate.size)}.",
        )
    bigger = "the inbox copy" if duplicate.size > keeper.size else "the library copy"
    return ToolFinding(
        "size",
        label,
        DIFFERENT,
        f"{bigger} is bigger",
        f"{human_bytes(duplicate.size)} in the inbox against {human_bytes(keeper.size)} in "
        f"the library. For the same recording, bigger usually means less compressed.",
    )


def _media_finding(facts: list[FactRow], kind: str) -> ToolFinding | None:
    """ffprobe/exiftool: the differences that decide which copy you want."""
    if kind not in {"audio", "video", "image"}:
        return None
    tool = "exiftool" if kind == "image" else "ffprobe"
    measured = [fact for fact in facts if fact.label not in _FILE_FACTS]
    if not measured:
        return ToolFinding(
            tool,
            tool,
            UNAVAILABLE,
            "could not read either copy",
            "The binary is missing, or neither file could be decoded.",
        )
    differing = [fact for fact in measured if not fact.same]
    if not differing:
        return ToolFinding(
            tool,
            tool,
            SAME,
            "same content, measured",
            "Duration, format and dimensions all match. Whatever the bytes say, these are "
            "the same recording.",
        )
    names = ", ".join(fact.label.lower() for fact in differing)
    return ToolFinding(
        tool,
        tool,
        DIFFERENT,
        f"differs in {names}",
        "The table below has both values side by side; that is the difference worth "
        "deciding on.",
    )


# --- the facts ------------------------------------------------------------

#  Read off the filesystem rather than measured by a media tool.
_FILE_FACTS = ("Name", "Folder", "Size", "Last modified")
#  Facts that differ between any two copies of anything and say nothing about
#  which one you want. A duplicate always has a different path, and a copy
#  almost always has a different timestamp.
_UNINFORMATIVE = ("Name", "Folder", "Last modified")


def _facts(
    settings: Settings, duplicate: Item, keeper: Item, left: Path, right: Path
) -> list[FactRow]:
    facts = [
        FactRow("Name", left.name, right.name),
        FactRow("Folder", _folder(duplicate.relpath), _folder(keeper.relpath)),
        FactRow("Size", human_bytes(duplicate.size), human_bytes(keeper.size)),
        FactRow("Last modified", _stamp(duplicate.mtime_ns), _stamp(keeper.mtime_ns)),
    ]
    kind = kind_for(left)
    reader = _read_image if kind == "image" else _read_media
    if kind in {"audio", "video", "image"}:
        facts.extend(_pair_up(reader(left, settings), reader(right, settings)))
    return facts


def _pair_up(left: dict[str, str], right: dict[str, str]) -> list[FactRow]:
    """Every property either side reported, in the order the first one gave."""
    labels = list(left) + [label for label in right if label not in left]
    return [FactRow(label, left.get(label, "—"), right.get(label, "—")) for label in labels]


def _read_media(path: Path, settings: Settings) -> dict[str, str]:
    from librairy.tools.ffprobe import probe

    result = _safely(lambda: probe(path, settings))
    if result is None or not result.ok or not isinstance(result.data, dict):
        return {}
    duration = result.data.get("duration")
    facts: dict[str, str] = {}
    if isinstance(duration, int | float) and duration > 0:
        facts["Duration"] = _duration(float(duration))
    if result.data.get("format_name"):
        facts["Container"] = str(result.data["format_name"]).split(",")[0]
    streams = result.data.get("streams") or ()
    video = _first_stream(streams, "video")
    audio = _first_stream(streams, "audio")
    if video:
        if video.get("width") and video.get("height"):
            facts["Resolution"] = f"{video['width']}×{video['height']}"
        if video.get("codec_name"):
            facts["Video codec"] = str(video["codec_name"])
    if audio:
        if audio.get("codec_name"):
            facts["Audio codec"] = str(audio["codec_name"])
        if audio.get("sample_rate"):
            facts["Sample rate"] = f"{int(audio['sample_rate']) // 1000} kHz"
        if audio.get("channels"):
            facts["Channels"] = str(audio["channels"])
    # ffprobe's format-level bit_rate is dropped by our parser, and the stream
    # one is often absent for VBR. The average over the whole file is the
    # honest number anyway, and it is the one that answers "which rip is this?"
    size = _safely(lambda: path.stat().st_size)
    if isinstance(duration, int | float) and duration > 0 and size:
        facts["Average bitrate"] = f"{round(size * 8 / duration / 1000)} kbps"
    return facts


def _read_image(path: Path, settings: Settings) -> dict[str, str]:
    from librairy.tools.exiftool import extract

    result = _safely(lambda: extract(path, settings))
    if result is None or not result.ok or not isinstance(result.data, dict):
        return {}
    tags = result.data.get("tags") or {}
    facts: dict[str, str] = {}
    if tags.get("ImageWidth") and tags.get("ImageHeight"):
        facts["Resolution"] = f"{tags['ImageWidth']}×{tags['ImageHeight']}"
    for label, key in (("Format", "FileType"), ("Colour space", "ColorSpace")):
        if tags.get(key):
            facts[label] = str(tags[key])
    if result.data.get("camera"):
        facts["Camera"] = str(result.data["camera"])
    if result.data.get("created_at"):
        facts["Taken"] = str(result.data["created_at"])
    if result.data.get("gps_latitude") is not None:
        facts["Location"] = "recorded in the file"
    return facts


def _first_stream(streams: Any, codec_type: str) -> dict[str, Any] | None:
    for stream in streams or ():
        if isinstance(stream, dict) and stream.get("codec_type") == codec_type:
            return stream
    return None


def _safely(call):  # noqa: ANN001, ANN202
    """A comparison is advice; no missing binary is worth an error page."""
    try:
        return call()
    except Exception:  # noqa: BLE001 - every failure here means "no fact"
        return None


# --- the conclusion -------------------------------------------------------


def _conclude(
    findings: list[ToolFinding], facts: list[FactRow], duplicate: Item, keeper: Item
) -> tuple[str, str, str]:
    by_tool = {finding.tool: finding for finding in findings}
    identical = by_tool["fingerprint"].verdict == SAME
    disagreement = by_tool["rmlint"].verdict == DIFFERENT
    differences = [fact for fact in facts if not fact.same and fact.label not in _UNINFORMATIVE]

    if identical and not disagreement:
        return (
            "identical",
            "Every byte matches. There is nothing in the inbox copy that the library copy "
            "does not already have.",
            "Quarantine the inbox copy. Nothing is deleted — it moves to the quarantine "
            "folder and can be restored from the Quarantine page.",
        )
    if identical and disagreement:
        return (
            "unclear",
            "The hashes match but rmlint did not pair these files, and two detectors "
            "disagreeing is reason enough to look before moving anything.",
            "Compare the previews below. Nothing has been staged for you.",
        )
    if differences:
        better = _better_copy(differences, duplicate, keeper)
        return (
            "similar",
            "Not the same bytes. These look like two versions of the same thing, "
            f"differing in {', '.join(fact.label.lower() for fact in differences)}.",
            better,
        )
    return (
        "unclear",
        "The two copies could not be told apart on anything measurable, and their bytes "
        "differ.",
        "Compare the previews below and decide by eye.",
    )


def _better_copy(differences: list[FactRow], duplicate: Item, keeper: Item) -> str:
    """Which copy looks like the better one, with the reason. Never acts on it."""
    resolution = next((fact for fact in differences if fact.label == "Resolution"), None)
    if resolution is not None:
        inbox_pixels = _pixels(resolution.inbox)
        library_pixels = _pixels(resolution.library)
        if inbox_pixels and library_pixels and inbox_pixels != library_pixels:
            if inbox_pixels > library_pixels:
                return (
                    f"The inbox copy is the larger picture ({resolution.inbox} against "
                    f"{resolution.library}). Reject the quarantine proposal to keep both — "
                    f"LibrAIry never overwrites, so the better copy is filed alongside and "
                    f"the old one stays where it is until you remove it yourself."
                )
            return (
                f"The library copy is the larger picture ({resolution.library} against "
                f"{resolution.inbox}), so the inbox one adds nothing. Quarantine it."
            )
    if duplicate.size and keeper.size and duplicate.size != keeper.size:
        if duplicate.size > keeper.size:
            return (
                "The inbox copy is the bigger file, which for the same recording usually "
                "means the better one. Reject the quarantine proposal to keep both."
            )
        return (
            "The library copy is the bigger file, so the inbox one is most likely a "
            "lower-quality version of what you already have."
        )
    return "Compare the previews below and decide which one you would rather keep."


def _pixels(value: str) -> int:
    try:
        width, height = value.replace("×", "x").split("x")
        return int(width) * int(height)
    except (ValueError, AttributeError):
        return 0


# --- storage --------------------------------------------------------------


def record_reports(conn: sqlite3.Connection, settings: Settings, candidates) -> int:
    """One report per candidate pair, replacing any earlier one for that pair."""
    written = 0
    for candidate in candidates:
        rmlint = SAME if candidate.status == "confirmed" else DIFFERENT
        if candidate.reason == "exact_duplicate_no_rmlint":
            rmlint = NOT_ASKED
        report = compare(conn, settings, candidate.duplicate, candidate.keeper, rmlint=rmlint)
        save_report(conn, report)
        written += 1
    return written


def record_similar_reports(conn: sqlite3.Connection, settings: Settings) -> int:
    """Reports for the pairs only czkawka found.

    These never match on their bytes, so the exact pass never sees them — and
    they are the pairs where a comparison earns its keep. Two encodes of one
    song, a screenshot and its resize: the question is which one you want, and
    that is answered by the resolution and the bitrate, not by a hash.
    """
    rows = conn.execute(
        """
        SELECT f.item_id, f.similar_item_id
        FROM similar_media_flags f
        JOIN items a ON a.id = f.item_id
        JOIN items b ON b.id = f.similar_item_id
        WHERE f.status = 'review'
          AND a.missing_since IS NULL AND b.missing_since IS NULL
          AND (a.root = 'inbox' OR b.root = 'inbox')
        """
    ).fetchall()
    written = 0
    for row in rows:
        left = _item(conn, int(row["item_id"]))
        right = _item(conn, int(row["similar_item_id"]))
        if left is None or right is None:
            continue
        # The inbox copy is the one being decided about, so it is always the
        # left-hand column; a report keyed the other way round would render
        # "In your inbox" over a file that has been filed for years.
        duplicate, keeper = (left, right) if left.root == "inbox" else (right, left)
        if duplicate.root == keeper.root:
            continue
        save_report(conn, compare(conn, settings, duplicate, keeper, rmlint=NOT_ASKED))
        written += 1
    return written


def _item(conn: sqlite3.Connection, item_id: int) -> Item | None:
    row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if row is None:
        return None
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


def save_report(conn: sqlite3.Connection, report: DuplicateReport) -> None:
    conn.execute(
        """
        INSERT INTO duplicate_reports(item_id, other_id, payload, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(item_id, other_id) DO UPDATE SET payload=excluded.payload,
                                                     created_at=excluded.created_at
        """,
        (report.item_id, report.other_id, json.dumps(asdict(report)), report.checked_at),
    )


def reports_for_item(conn: sqlite3.Connection, item_id: int) -> list[DuplicateReport]:
    rows = conn.execute(
        "SELECT payload FROM duplicate_reports WHERE item_id=? ORDER BY created_at DESC",
        (item_id,),
    ).fetchall()
    return [_from_payload(row["payload"]) for row in rows]


def items_with_reports(conn: sqlite3.Connection, item_ids: list[int]) -> set[int]:
    """Which of these items have a comparison, in one query rather than N."""
    if not item_ids:
        return set()
    placeholders = ",".join("?" for _ in item_ids)
    rows = conn.execute(
        f"SELECT DISTINCT item_id FROM duplicate_reports WHERE item_id IN ({placeholders})",  # noqa: S608
        item_ids,
    )
    return {int(row["item_id"]) for row in rows}


def _from_payload(payload: str) -> DuplicateReport:
    data = json.loads(payload)
    return DuplicateReport(
        item_id=int(data["item_id"]),
        other_id=int(data["other_id"]),
        verdict=str(data["verdict"]),
        summary=str(data["summary"]),
        recommendation=str(data["recommendation"]),
        findings=tuple(ToolFinding(**finding) for finding in data.get("findings", ())),
        facts=tuple(FactRow(**fact) for fact in data.get("facts", ())),
        checked_at=str(data.get("checked_at", "")),
    )


# --- small shared helpers -------------------------------------------------


def _option(conn: sqlite3.Connection, key: str, *, default: bool) -> bool:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return bool(json.loads(row["value"]))
    except (TypeError, ValueError):
        return default


def _worker_flag(conn: sqlite3.Connection, key: str, *, default: bool) -> bool:
    row = conn.execute("SELECT value FROM worker_state WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return bool(json.loads(row["value"]))
    except (TypeError, ValueError):
        return default


def _path_for(settings: Settings, item: Item) -> Path:
    root = settings.library_dir if item.root == "library" else settings.inbox_dir
    if item.root == "quarantine":
        root = settings.quarantine_dir
    return root / item.relpath


def _folder(relpath: str) -> str:
    parent = relpath.rsplit("/", 1)[0] if "/" in relpath else ""
    return parent or "the top level"


def _stamp(mtime_ns: int | None) -> str:
    if not mtime_ns:
        return "unknown"
    return datetime.fromtimestamp(mtime_ns / 1_000_000_000, tz=UTC).strftime("%Y-%m-%d %H:%M")


def _duration(seconds: float) -> str:
    minutes, remainder = divmod(int(round(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{remainder:02d}"
    return f"{minutes}:{remainder:02d}"


__all__ = [
    "DIFFERENT",
    "NOT_ASKED",
    "SAME",
    "SIMILAR",
    "UNAVAILABLE",
    "DuplicateReport",
    "FactRow",
    "ToolFinding",
    "compare",
    "items_with_reports",
    "record_reports",
    "record_similar_reports",
    "reports_for_item",
    "save_report",
]
