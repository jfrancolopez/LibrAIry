"""What a Format Policy would actually be about, measured before it does anything.

A policy nobody can see the consequences of is a policy nobody can safely set.
"Prefer MP3" is a sentence; *eighty-four recordings you already have in both,
three point two gigabytes of FLAC, and four hundred and thirty recordings where
the MP3 does not exist and would have to be made* is a decision.

So this is a **read-only dry run**. It creates no plans, no optimization jobs,
no proposals and no files, and it never writes anything except its own cached
result. Every number comes from the index — sizes and paths LibrAIry already
recorded — and the counting happens in SQL rather than by loading a hundred
thousand rows into Python to add them up.

Three distinctions the report refuses to blur, because each one is a way a
storage claim becomes a lie:

**Existing representation against potential conversion.** A recording you
already have as both FLAC and MP3 is a *choice* — both files are on the disk
now. A FLAC with no MP3 is a *conversion*, and the MP3 does not exist. Adding
those two together and calling the total "MP3 coverage" would report a library
that is not there.

**Reduction against reclaimed.** LibrAIry does not delete. Preferring MP3 does
not remove a FLAC, and neither does this analysis; what a preference can
eventually lead to is a file being set aside, by an explicit decision, and
removed later by a person. The wording says *would eventually leave active
representation storage*, never *saved*, and the same distinction Storage
Optimization already draws is reused rather than reinvented.

**Related against redundant.** Three hundred RAW files that also have JPEG
renders are three hundred *pairs*, and a program that called them redundant
would be making an argument about somebody's photographs. The relationship
counts are reported and nothing is recommended about them.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field

from librairy.format_policy import (
    KNOWN_FORMATS,
    SECTION_COVERS,
    SECTION_LABEL,
    SECTIONS,
    preferred_for,
    protected_folders,
)
from librairy.humanize import human_bytes

#  Where the last measurement lives. One row, replaced each time: this is a
#  snapshot, and keeping a history of snapshots would invite somebody to read
#  a trend into numbers that only move when the analysis is re-run.
SETTING_KEY = "format_policy.impact"

#  How many folders one section names before it starts counting instead. Same
#  reasoning as every other bounded list in LibrAIry: a report that lists ten
#  thousand folders is not a report.
SHOWN = 10

#  A suffix is allowed into generated SQL only if it looks like this. The lists
#  are module constants rather than input, so this is belt-and-braces — but the
#  moment a format name comes from anywhere else, this is what stops it.
SAFE_SUFFIX = re.compile(r"^[a-z0-9]{1,8}$")

#  What a transformation costs, stated objectively and never as a quality
#  judgement. "No quality difference" is a claim this report is not entitled to
#  make, and "better" is not a fact about a file.
TRANSFORM_DISCLOSURE = {
    ("wav", "flac"): "lossless — the audio is re-packed, not re-encoded",
    ("aiff", "flac"): "lossless — the audio is re-packed, not re-encoded",
    ("flac", "mp3"): "lossy — audio is discarded and cannot be recovered",
    ("wav", "mp3"): "lossy — audio is discarded and cannot be recovered",
    ("alac", "mp3"): "lossy — audio is discarded and cannot be recovered",
}
LOSSY_DEFAULT = "lossy — the original data is not recoverable from the result"
LOSSLESS_DEFAULT = "lossless — the original data can be reconstructed"

#  Formats that carry the whole signal. Anything else is already lossy, and a
#  conversion out of it is lossy again whatever the destination.
LOSSLESS_AUDIO = frozenset({"wav", "aiff", "aif", "flac", "alac", "ape", "wv"})


def disclosure(source: str, target: str) -> str:
    """What changes, in a sentence that claims nothing about quality."""
    named = TRANSFORM_DISCLOSURE.get((source, target))
    if named:
        return named
    if target in LOSSLESS_AUDIO and source in LOSSLESS_AUDIO:
        return LOSSLESS_DEFAULT
    return LOSSY_DEFAULT


@dataclass(frozen=True)
class Section:
    """One category's answer, in the three parts that must not be added up."""

    category: str
    label: str
    preferred: str = ""
    #  Things that already exist in the preferred format *and* another one.
    both_count: int = 0
    preferred_bytes: int = 0
    other_bytes: int = 0
    #  Things that exist only in some other format. The preferred copy would
    #  have to be made, and making it is a separate permission.
    convertible_count: int = 0
    convertible_bytes: int = 0
    transform_allowed: bool | None = None
    #  Already only the preferred format. Nothing to decide.
    settled_count: int = 0
    folders: list[dict[str, object]] = field(default_factory=list)
    folders_more: int = 0
    disclosures: list[str] = field(default_factory=list)


def analyse(conn: sqlite3.Connection, settings=None) -> dict:  # noqa: ANN001, ARG001
    """Measure what the current policy is about. Writes nothing but its result.

    Deliberately an explicit action rather than something a page does while
    rendering. It reads every indexed library row, and a GET that walks the
    whole index is a page that gets slower the more somebody owns.
    """
    from librairy.planner import utc_now
    from librairy.relationships import LIVE_PHOTO, RAW_RENDER

    scopes = protected_folders(conn)
    guard = _protected_sql(scopes)
    report = {
        "measured_at": utc_now(),
        "item_count": _library_count(conn),
        "sections": [
            _section(conn, name, guard) for name in SECTIONS
        ],
        "protected": _protected(conn, scopes),
        "relationships": [
            _relationship(conn, RAW_RENDER, "RAW file with a JPEG render",
                          "RAW files with JPEG renders", scopes),
            _relationship(conn, LIVE_PHOTO, "Live Photo", "Live Photos", scopes),
        ],
    }
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (SETTING_KEY, json.dumps(report)),
    )
    return report


def last(conn: sqlite3.Connection) -> dict | None:
    """The last measurement, or None if nobody has run one."""
    row = conn.execute(
        "SELECT value FROM settings WHERE key=?", (SETTING_KEY,)
    ).fetchone()
    if row is None:
        return None
    try:
        found = json.loads(str(row["value"]))
    except (TypeError, ValueError):
        return None
    return found if isinstance(found, dict) else None


def is_stale(conn: sqlite3.Connection, report: dict | None) -> bool:
    """Whether the library has moved on since this was measured.

    Keyed `item_count` rather than `items`, because a template asking for
    `impact.items` gets the dictionary's own `.items` method — which renders as
    `<built-in method items>` where a number should be, and is exactly the kind
    of wrongness that makes a whole report look untrustworthy.

    A file count, not a hash of everything: it is cheap, it is honest about
    what it can detect, and a report that is a week old with the same count is
    still a snapshot rather than execution truth. What this stops is a stale
    estimate being read as current after somebody has imported a camera card.
    """
    if not report:
        return False
    return int(report.get("item_count") or 0) != _library_count(conn)


def _library_count(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM items WHERE root='library' AND missing_since IS NULL"
        ).fetchone()[0]
    )


def _protected_sql(scopes: list[str]) -> str:
    """A SQL predicate for "inside one of the protected folders".

    `LIKE` with the folder and a separator, never a bare prefix: `Photos/Wedding`
    must not cover `Photos/WeddingExports`, and it is the trailing slash that
    makes the difference. SQLite's LIKE is ASCII case-insensitive, which is the
    same casefolding the Python path check does.
    """
    if not scopes:
        return "0"
    parts = []
    for scope in scopes:
        safe = scope.replace("'", "''")
        parts.append(f"(i.relpath = '{safe}' OR i.relpath LIKE '{safe}/%')")
    return "(" + " OR ".join(parts) + ")"


def _suffixes(category: str) -> list[str]:
    found = sorted(KNOWN_FORMATS.get(category, frozenset()))
    return [suffix for suffix in found if SAFE_SUFFIX.match(suffix)]


def _section(conn: sqlite3.Connection, category: str, guard: str) -> dict:
    """One category, counted in SQL over the index.

    The grouping key is the same one `format_preference.equivalent` uses for
    the library's own naming — same folder, same stem — so the page that counts
    and the code that acts cannot disagree about which files are two copies of
    one thing. Identity is never inferred from format here; format only names
    the copies once identity has grouped them.
    """
    suffixes = _suffixes(category)
    preferred = preferred_for(conn, category)
    if not suffixes:
        return _empty(category, preferred)
    ext_case = " ".join(
        f"WHEN lower(i.relpath) LIKE '%.{suffix}' THEN '{suffix}'" for suffix in suffixes
    )
    likes = " OR ".join(f"lower(i.relpath) LIKE '%.{suffix}'" for suffix in suffixes)
    tops = SECTION_COVERS.get(category, (category,))
    roots = " OR ".join(
        f"i.relpath LIKE '{top.replace(chr(39), chr(39) * 2)}/%'"
        for top in _library_folders(tops)
    )
    rows = conn.execute(
        f"""
        WITH copies AS (
          SELECT i.id AS id, i.relpath AS relpath, i.size AS size,
                 (CASE {ext_case} END) AS fmt,
                 {guard} AS protected
          FROM items i
          WHERE i.root='library' AND i.missing_since IS NULL
            AND ({likes}) AND ({roots})
        ),
        keyed AS (
          SELECT relpath, size, fmt, protected,
                 substr(relpath, 1, length(relpath) - length(fmt) - 1) AS stem
          FROM copies
        ),
        grouped AS (
          SELECT stem,
                 MAX(CASE WHEN protected THEN 1 ELSE 0 END) AS protected,
                 SUM(CASE WHEN fmt = ? THEN 1 ELSE 0 END) AS wanted,
                 COUNT(*) AS copies,
                 SUM(CASE WHEN fmt = ? THEN size ELSE 0 END) AS wanted_bytes,
                 SUM(CASE WHEN fmt <> ? THEN size ELSE 0 END) AS other_bytes
          FROM keyed GROUP BY stem
        )
        SELECT
          SUM(CASE WHEN NOT protected AND wanted > 0 AND copies > wanted
                   THEN 1 ELSE 0 END) AS both_count,
          SUM(CASE WHEN NOT protected AND wanted > 0 AND copies > wanted
                   THEN wanted_bytes ELSE 0 END) AS preferred_bytes,
          SUM(CASE WHEN NOT protected AND wanted > 0 AND copies > wanted
                   THEN other_bytes ELSE 0 END) AS other_bytes,
          SUM(CASE WHEN NOT protected AND wanted = 0 THEN 1 ELSE 0 END)
            AS convertible_count,
          SUM(CASE WHEN NOT protected AND wanted = 0 THEN other_bytes ELSE 0 END)
            AS convertible_bytes,
          SUM(CASE WHEN NOT protected AND wanted > 0 AND copies = wanted
                   THEN 1 ELSE 0 END) AS settled_count
        FROM grouped
        """,  # noqa: S608 - clauses are built from module constants, checked by SAFE_SUFFIX
        (preferred, preferred, preferred),
    ).fetchone()
    section = _empty(category, preferred)
    if rows is None or not preferred:
        #  With no preference there is nothing to be "in the preferred format",
        #  and reporting zeros against a preference nobody set would read as a
        #  recommendation to set one.
        return section
    from librairy.format_policy import resolve

    policy = resolve(conn, f"{_library_folders(tops)[0]}/x.{suffixes[0]}")
    section.update(
        {
            "both_count": int(rows["both_count"] or 0),
            "preferred_bytes": int(rows["preferred_bytes"] or 0),
            "other_bytes": int(rows["other_bytes"] or 0),
            "convertible_count": int(rows["convertible_count"] or 0),
            "convertible_bytes": int(rows["convertible_bytes"] or 0),
            "settled_count": int(rows["settled_count"] or 0),
            "transform_allowed": policy.allow_lossy,
            "folders": _folders(conn, category, suffixes, preferred, guard),
            "disclosures": _disclosures(conn, category, suffixes, preferred, guard),
        }
    )
    section["preferred_bytes_label"] = human_bytes(section["preferred_bytes"])
    section["other_bytes_label"] = human_bytes(section["other_bytes"])
    section["convertible_bytes_label"] = human_bytes(section["convertible_bytes"])
    return section


def _empty(category: str, preferred: str) -> dict:
    return {
        "category": category,
        "label": SECTION_LABEL.get(category, category.title()),
        "preferred": preferred,
        "preferred_label": preferred.upper() if preferred else "",
        "both_count": 0,
        "preferred_bytes": 0,
        "preferred_bytes_label": human_bytes(0),
        "other_bytes": 0,
        "other_bytes_label": human_bytes(0),
        "convertible_count": 0,
        "convertible_bytes": 0,
        "convertible_bytes_label": human_bytes(0),
        "settled_count": 0,
        "transform_allowed": None,
        "folders": [],
        "folders_more": 0,
        "disclosures": [],
    }


#  Where each category actually lives in a filed library. The report counts over
#  the library's own structure rather than over every file with a matching
#  extension, so an MP3 sitting beside a film in `Movies/` is not counted as a
#  music representation question.
_LIBRARY_FOLDER = {
    "music": "Music",
    "music_videos": "Music Videos",
    "movies": "Movies",
    "shows": "Shows",
    "photos": "Photos",
    "documents": "Documents",
    "books": "Books",
}


def _library_folders(categories: tuple[str, ...]) -> list[str]:
    return [_LIBRARY_FOLDER[name] for name in categories if name in _LIBRARY_FOLDER]


def _folders(
    conn: sqlite3.Connection,
    category: str,
    suffixes: list[str],
    preferred: str,
    guard: str,
) -> list[dict[str, object]]:
    """Which folders the existing-representation count is actually in.

    Bounded, and only for the *existing* half: a list of folders where a
    conversion could happen is a list of work nobody has agreed to.
    """
    ext_case = " ".join(
        f"WHEN lower(i.relpath) LIKE '%.{suffix}' THEN '{suffix}'" for suffix in suffixes
    )
    likes = " OR ".join(f"lower(i.relpath) LIKE '%.{suffix}'" for suffix in suffixes)
    tops = _library_folders(SECTION_COVERS.get(category, (category,)))
    roots = " OR ".join(f"i.relpath LIKE '{top}/%'" for top in tops)
    rows = conn.execute(
        f"""
        WITH copies AS (
          SELECT i.relpath AS relpath, i.size AS size,
                 (CASE {ext_case} END) AS fmt, {guard} AS protected
          FROM items i
          WHERE i.root='library' AND i.missing_since IS NULL
            AND ({likes}) AND ({roots})
        ),
        keyed AS (
          SELECT size, fmt, protected,
                 substr(relpath, 1, length(relpath) - length(fmt) - 1) AS stem,
                 rtrim(substr(relpath, 1, length(relpath) - length(fmt) - 1),
                       replace(substr(relpath, 1, length(relpath) - length(fmt) - 1),
                               '/', '')) AS folder
          FROM copies
        ),
        grouped AS (
          SELECT stem, MIN(folder) AS folder,
                 MAX(CASE WHEN protected THEN 1 ELSE 0 END) AS protected,
                 SUM(CASE WHEN fmt = ? THEN 1 ELSE 0 END) AS wanted,
                 COUNT(*) AS copies,
                 SUM(CASE WHEN fmt <> ? THEN size ELSE 0 END) AS other_bytes
          FROM keyed GROUP BY stem
        )
        SELECT rtrim(folder, '/') AS folder, COUNT(*) AS n,
               SUM(other_bytes) AS bytes
        FROM grouped
        WHERE NOT protected AND wanted > 0 AND copies > wanted
        GROUP BY folder ORDER BY bytes DESC, folder LIMIT ?
        """,  # noqa: S608 - clauses are built from module constants
        (preferred, preferred, SHOWN + 1),
    ).fetchall()
    return [
        {
            "folder": str(row["folder"]),
            "count": int(row["n"]),
            "bytes_label": human_bytes(int(row["bytes"] or 0)),
        }
        for row in rows[:SHOWN]
    ]


def _disclosures(
    conn: sqlite3.Connection,
    category: str,
    suffixes: list[str],
    preferred: str,
    guard: str,
) -> list[str]:
    """What a conversion would do, per source format actually present.

    Written from the formats this library holds rather than from a table of
    every conversion imaginable: a sentence about WAV in a library with no WAV
    is noise, and noise is how the sentence about FLAC stops being read.
    """
    if not preferred:
        return []
    ext_case = " ".join(
        f"WHEN lower(i.relpath) LIKE '%.{suffix}' THEN '{suffix}'" for suffix in suffixes
    )
    likes = " OR ".join(f"lower(i.relpath) LIKE '%.{suffix}'" for suffix in suffixes)
    tops = _library_folders(SECTION_COVERS.get(category, (category,)))
    roots = " OR ".join(f"i.relpath LIKE '{top}/%'" for top in tops)
    rows = conn.execute(
        f"""
        SELECT (CASE {ext_case} END) AS fmt, COUNT(*) AS n
        FROM items i
        WHERE i.root='library' AND i.missing_since IS NULL
          AND ({likes}) AND ({roots}) AND NOT {guard}
        GROUP BY fmt ORDER BY n DESC
        """,  # noqa: S608 - clauses are built from module constants
    ).fetchall()
    found: list[str] = []
    for row in rows:
        source = str(row["fmt"] or "")
        if not source or source == preferred:
            continue
        found.append(
            f"{source.upper()} → {preferred.upper()}: {disclosure(source, preferred)}"
        )
    return found[:SHOWN]


def _protected(conn: sqlite3.Connection, scopes: list[str]) -> list[dict[str, object]]:
    """Each protected folder, counted and sized. No argument attached.

    Reported so the owner can see what they protected, never with a nudge to
    unprotect it. The whole point of a keepsake is that its size is not the
    interesting fact about it.
    """
    found: list[dict[str, object]] = []
    for scope in scopes:
        safe = scope.replace("'", "''")
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS n, COALESCE(SUM(size), 0) AS bytes FROM items i
            WHERE i.root='library' AND i.missing_since IS NULL
              AND (i.relpath = '{safe}' OR i.relpath LIKE '{safe}/%')
            """,  # noqa: S608 - the scope was validated when it was saved
        ).fetchone()
        found.append(
            {
                "folder": scope,
                "count": int(row["n"] or 0),
                "bytes_label": human_bytes(int(row["bytes"] or 0)),
            }
        )
    return found


def _relationship(
    conn: sqlite3.Connection, kind: str, singular: str, plural: str, scopes: list[str]
) -> dict[str, object]:
    """How many pairs of this kind exist, and how many are protected.

    Counted and named, never judged. Three hundred RAW files that also have
    JPEG renders are three hundred pairs; calling them redundant would be an
    argument about somebody's photographs dressed up as a measurement.
    """
    guard = _protected_sql(scopes).replace("i.relpath", "i2.relpath")
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS n,
               SUM(CASE WHEN EXISTS (
                     SELECT 1 FROM items i2
                     WHERE i2.id IN (r.low_item_id, r.high_item_id) AND {guard}
                   ) THEN 1 ELSE 0 END) AS protected
        FROM item_relationships r WHERE r.kind = ?
        """,  # noqa: S608 - the scope was validated when it was saved
        (kind,),
    ).fetchone()
    total = int(row["n"] or 0)
    protected = int(row["protected"] or 0)
    return {
        "kind": kind,
        "label": singular if total == 1 else plural,
        "count": total,
        "protected": protected,
        "unprotected": total - protected,
    }
