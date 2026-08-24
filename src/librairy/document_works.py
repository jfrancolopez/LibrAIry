"""One work, two files, and the three different things that can mean.

    Books/Frank Herbert/Dune/Dune.epub     EPUB · 1.2 MB
    Books/Frank Herbert/Dune/Dune.pdf      PDF  · 4.8 MB

Neither the exact-duplicate workflow nor the similar-media one can see this
pair. The bytes differ, so no fingerprint matches them; czkawka compares images,
video and audio, so nothing pairs two documents. They are the same book and
LibrAIry had nothing to say about it.

**Three concepts, and collapsing any two of them loses something.**

    exact duplicate     the same bytes — `audit_duplicates`, which knows what
                        rmlint said and can say so
    same work, other    the same ISBN or DOI in two containers. Both may be
    format              worth keeping: an EPUB reflows on a phone and a PDF
                        keeps the typesetting
    version or edition  the 2023 manual and the 2024 one; a preprint and the
                        published paper. **Not duplicates**, and nothing here
                        will treat them as such

**Identity, and nothing weaker.** A group exists only where two documents carry
the *same* ISBN or the *same* DOI — identifiers a publisher assigned, not
strings that resemble each other. Nothing here compares titles, and that single
refusal is what protects every case that matters: `Account Statement March
2024` and `Account Statement April 2024` share a title, a folder and a
template, and have no identifier at all, so they are never a group. A second
edition carries a second ISBN. A preprint that was never assigned the published
DOI keeps its own.

**No format is preferred.** The music preference is about music and stays
there: nobody has said whether they would rather keep an EPUB or a PDF, so the
row offers both and a way to keep both, with no badge on either. Borrowing the
MP3 preference here would be inventing an opinion out of an unrelated one.

What happens after the choice is the comparison machinery that already works —
the kept files stay, the rest wait for Commit and go to Quarantine, and nothing
is deleted. There is no document executor.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import PurePosixPath

from librairy.config import Settings

KIND = "document-formats"

#  Past this it is not a comparison. A work in nine containers is a question
#  about somebody's whole shelf, not about one book.
MAX_MEMBERS = 6

#  How many unread documents one audit run opens. Filing a document creates a
#  new item row, so a book that was identified in the inbox arrives in the
#  library unmeasured — and the audit is the pass that reads what is filed.
#  Bounded because it is two subprocesses per file: a library of ten thousand
#  PDFs is caught up over several runs rather than in one that never finishes.
MEASURE_PER_RUN = 200


@dataclass(frozen=True)
class Member:
    """One file of a work, and the facts that tell it from the others."""

    item_id: int
    relpath: str
    size: int
    facts: tuple[tuple[str, str], ...] = ()

    @property
    def name(self) -> str:
        return PurePosixPath(self.relpath).name

    @property
    def folder(self) -> str:
        return str(PurePosixPath(self.relpath).parent)

    @property
    def format(self) -> str:
        return PurePosixPath(self.relpath).suffix.lstrip(".").upper()


@dataclass(frozen=True)
class Work:
    """A work and the formats of it that are filed."""

    finding_id: int
    identifier: str
    scheme: str
    title: str
    members: tuple[Member, ...]

    @property
    def label(self) -> str:
        return "ISBN" if self.scheme == "isbn" else "DOI"

    @property
    def formats(self) -> str:
        return ", ".join(dict.fromkeys(member.format for member in self.members))

    @property
    def resolvable(self) -> bool:
        return len(self.members) >= 2


def is_work_finding(row: sqlite3.Row) -> bool:
    try:
        return row["kind"] == KIND
    except (KeyError, IndexError):
        return False


# --- finding the works ----------------------------------------------------------------


def measure_filed(
    conn: sqlite3.Connection, settings: Settings, *, limit: int = MEASURE_PER_RUN
) -> int:
    """Read the filed documents nobody has read yet. Returns how many.

    Called from the Library Audit, which is a background pass over files that
    are already on the shelf — the same place the music tags are read. Never
    from a request: `pdfinfo` and `pdftotext` are subprocesses.

    The gate is the fingerprint, so this covers both cases that matter: a
    document that has never been read, and one whose bytes changed since it
    was. Committing a document creates a new item row, which is why a book
    identified in the inbox needs reading again once it is filed.
    """
    from librairy.docmeta import cached_facts, facts_for_item
    from librairy.paths import PathValidationError, validate_relpath

    counted = 0
    for row in conn.execute(
        "SELECT id, relpath FROM items WHERE root='library' AND missing_since IS NULL"
        " AND (relpath LIKE '%.pdf' OR relpath LIKE '%.epub') ORDER BY id"
    ).fetchall():
        if counted >= limit:
            break
        relpath = str(row["relpath"])
        try:
            path = validate_relpath(settings.library_dir, relpath, kind="finding")
        except PathValidationError:
            continue
        if not path.is_file() or cached_facts(conn, int(row["id"]), path) is not None:
            continue
        facts_for_item(conn, settings, int(row["id"]), path)
        counted += 1
    return counted


def detect(conn: sqlite3.Connection) -> list:
    """One finding per identifier held by two or more filed documents.

    Read from the metadata cache, so this costs a query and opens nothing: a
    document's ISBN was recorded when it was analysed, against the bytes it was
    read from. A file re-scanned since then has a different fingerprint, its
    cached identity is not returned, and it simply does not take part — which
    is the right answer, because nobody has read the new bytes.
    """
    from librairy.audit import Finding
    from librairy.models import EvidenceEntry
    from librairy.tools.common import DOCUMENT_TOOL

    works: dict[tuple[str, str], list[sqlite3.Row]] = {}
    titles: dict[tuple[str, str], str] = {}
    for row in conn.execute(
        """
        SELECT i.id, i.relpath, i.size, m.payload
        FROM item_metadata m
        JOIN items i ON i.id = m.item_id AND i.fingerprint = m.fingerprint
        WHERE m.tool = ? AND i.root = 'library' AND i.missing_since IS NULL
        ORDER BY i.relpath
        """,
        (DOCUMENT_TOOL,),
    ):
        found = _identity(str(row["payload"]))
        if found is None:
            continue
        scheme, identifier, title = found
        works.setdefault((scheme, identifier), []).append(row)
        titles.setdefault((scheme, identifier), title)

    dismissed = _dismissed(conn)
    findings = []
    for (scheme, identifier), rows in sorted(works.items()):
        members = _distinct(rows)
        if len(members) < 2 or len(members) > MAX_MEMBERS:
            continue
        if dismissed.get((scheme, identifier)) == _member_key(conn, members):
            #  Answered "keep both" about these exact files. Replace one of
            #  them and the key changes, so the question comes back — which is
            #  right, because nobody has been asked about the new file.
            continue
        title = titles[(scheme, identifier)] or PurePosixPath(members[0]["relpath"]).stem
        formats = ", ".join(
            dict.fromkeys(
                PurePosixPath(str(row["relpath"])).suffix.lstrip(".").upper()
                for row in members
            )
        )
        findings.append(
            Finding(
                relpath=str(members[0]["relpath"]),
                kind=KIND,
                severity="review",
                summary=f"{title} is filed in {len(members)} formats: {formats}.",
                evidence=[
                    EvidenceEntry(
                        "document", scheme, identifier, 0.95,
                    ),
                    *[
                        EvidenceEntry(
                            "filesystem", "also filed as", str(row["relpath"]), 0.9
                        )
                        for row in members[1:]
                    ],
                ],
            )
        )
    return findings


def _dismissed(conn: sqlite3.Connection) -> dict[tuple[str, str], str]:
    return {
        (str(row["scheme"]), str(row["identifier"])): str(row["fingerprints"])
        for row in conn.execute(
            "SELECT scheme, identifier, fingerprints FROM document_work_choices"
        )
    }


def _member_key(conn: sqlite3.Connection, rows) -> str:  # noqa: ANN001
    """The members' content hashes, sorted and joined.

    An answer about two specific files, not about an identifier: a book whose
    PDF was replaced by a better scan is a comparison nobody has made.
    """
    found = []
    for row in rows:
        item = conn.execute(
            "SELECT fingerprint FROM items WHERE id=?", (int(row["id"]),)
        ).fetchone()
        found.append(str(item["fingerprint"] or "") if item else "")
    return "|".join(sorted(found))


def keep_all(
    conn: sqlite3.Connection, settings: Settings, finding_id: int
) -> None:
    """Answer "keep every format", which has no filesystem work in it.

    No plan is made: an empty one would appear on Commit, be committed, and
    write a History entry saying a decision was carried out. What has to happen
    instead is that the next audit does not ask again, which is what the
    dismissal row is for.
    """
    from librairy.corrections import load_finding
    from librairy.planner import utc_now

    row = load_finding(conn, finding_id)
    view = compare(conn, settings, row)
    if view is None:
        return
    rows = detect_rows(conn, view.scheme, view.identifier)
    conn.execute(
        "INSERT INTO document_work_choices(scheme, identifier, fingerprints, created_at)"
        " VALUES (?, ?, ?, ?)"
        " ON CONFLICT(scheme, identifier) DO UPDATE SET"
        " fingerprints=excluded.fingerprints, created_at=excluded.created_at",
        (view.scheme, view.identifier, _member_key(conn, rows), utc_now()),
    )
    conn.execute(
        "UPDATE audit_findings SET status='kept', updated_at=? WHERE id=?",
        (utc_now(), finding_id),
    )


def _identity(payload: str) -> tuple[str, str, str] | None:
    """The publisher's identifier for this document, or None.

    ISBN first, because a book that also prints a DOI is still that book. A
    document with neither is not part of any work group — which is deliberate,
    and is what keeps twelve monthly statements from the same bank apart.
    """
    import json

    try:
        found = json.loads(payload)
    except (TypeError, ValueError):
        return None
    if not isinstance(found, dict):
        return None
    title = str(found.get("title") or "")
    for scheme in ("isbn", "doi"):
        value = str(found.get(scheme) or "").strip()
        if value:
            return scheme, value, title
    return None


def _distinct(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """One row per *format*, and only where the bytes differ.

    Two files with the same bytes are the exact-duplicate workflow's question
    and it can answer it properly. Two PDFs of one ISBN are the same format
    twice — a re-download, or a second scan — and this row has nothing to say
    about which; that is what the duplicate and similar workflows are for.
    """
    seen: dict[str, sqlite3.Row] = {}
    for row in rows:
        suffix = PurePosixPath(str(row["relpath"])).suffix.lower()
        seen.setdefault(suffix, row)
    return [seen[key] for key in sorted(seen)]


# --- reading one ------------------------------------------------------------------------


def compare(
    conn: sqlite3.Connection, settings: Settings, row: sqlite3.Row
) -> Work | None:
    """The formats as they are filed now, with the facts each one carries.

    Rebuilt from the cache rather than from the finding's evidence, which is a
    statement about the moment the audit ran. A format deleted by hand since
    then is not something to choose between. Nothing is opened here.
    """
    from librairy.docmeta import cached_facts
    from librairy.paths import PathValidationError, validate_relpath

    if not is_work_finding(row):
        return None
    identity = _finding_identity(row)
    if identity is None:
        return None
    scheme, identifier = identity
    members: list[Member] = []
    title = ""
    for found in detect_rows(conn, scheme, identifier):
        relpath = str(found["relpath"])
        try:
            path = validate_relpath(settings.library_dir, relpath, kind="finding")
        except PathValidationError:
            continue
        if not path.is_file():
            continue
        facts = cached_facts(conn, int(found["id"]), path)
        title = title or (facts.title if facts else "")
        members.append(
            Member(
                item_id=int(found["id"]),
                relpath=relpath,
                size=int(found["size"] or 0),
                facts=_facts(facts, relpath, int(found["size"] or 0)),
            )
        )
    members = _one_per_format(members)
    if len(members) < 2:
        return None
    return Work(
        finding_id=int(row["id"]),
        identifier=identifier,
        scheme=scheme,
        title=title or PurePosixPath(members[0].relpath).stem,
        members=tuple(members),
    )


def detect_rows(
    conn: sqlite3.Connection, scheme: str, identifier: str
) -> list[sqlite3.Row]:
    """Filed documents whose cached identity is exactly this identifier.

    The join on `i.fingerprint = m.fingerprint` is the gate: a document
    re-scanned since it was read has cached identity about bytes it no longer
    has, and it drops out of every work group until somebody reads it again.
    """
    from librairy.tools.common import DOCUMENT_TOOL

    found = []
    for row in conn.execute(
        """
        SELECT i.id, i.relpath, i.size, m.payload
        FROM item_metadata m
        JOIN items i ON i.id = m.item_id AND i.fingerprint = m.fingerprint
        WHERE m.tool = ? AND i.root = 'library' AND i.missing_since IS NULL
        ORDER BY i.relpath
        """,
        (DOCUMENT_TOOL,),
    ):
        identity = _identity(str(row["payload"]))
        if identity is not None and identity[:2] == (scheme, identifier):
            found.append(row)
    return found


def _one_per_format(members: list[Member]) -> list[Member]:
    seen: dict[str, Member] = {}
    for member in members:
        seen.setdefault(member.format, member)
    return [seen[key] for key in sorted(seen)]


def _finding_identity(row: sqlite3.Row) -> tuple[str, str] | None:
    from librairy.proposals import decode_evidence

    try:
        entries = decode_evidence(row["evidence"]) if row["evidence"] else []
    except (TypeError, ValueError):
        return None
    for entry in entries:
        if entry.source == "document" and entry.field in {"isbn", "doi"}:
            return entry.field, str(entry.detail)
    return None


def _facts(facts, relpath: str, size: int) -> tuple[tuple[str, str], ...]:  # noqa: ANN001
    """What is known about this file, measured and never judged.

    No `better`, no `recommended` and no preferred badge — nobody has said
    which document format they would rather keep, and the music preference is
    about music.
    """
    from librairy.humanize import human_bytes

    found: list[tuple[str, str]] = [
        ("Format", PurePosixPath(relpath).suffix.lstrip(".").upper()),
        ("Size", human_bytes(size)),
    ]
    if facts is None:
        return tuple(found)
    if facts.pages:
        found.append(("Pages", str(facts.pages)))
    if facts.author:
        found.append(("Author", facts.author))
    if facts.year:
        found.append(("Year", str(facts.year)))
    if facts.scanned:
        found.append(("Text", "no text layer — this is a scan"))
    return tuple(found)


# --- the decision -----------------------------------------------------------------------


def resolve(
    conn: sqlite3.Connection, settings: Settings, finding_id: int, keep: list[str]
) -> str:
    """Keep the named formats; the rest wait for Commit, then Quarantine.

    The same shape as every other comparison, and deliberately the same
    machinery: `similar_media.set_aside` builds the plan, so there is one
    spelling of "these go and that stays" rather than a document-flavoured
    copy of it. Keeping everything is a real answer that makes no plan.
    """
    from librairy.corrections import CorrectionRefused, load_finding
    from librairy.similar_media import set_aside

    row = load_finding(conn, finding_id)
    view = compare(conn, settings, row)
    if view is None:
        raise CorrectionRefused("there is only one format of this left")
    known = {member.relpath for member in view.members}
    kept = [relpath for relpath in dict.fromkeys(keep) if relpath in known]
    if len(kept) != len(set(keep)):
        raise CorrectionRefused("one of those files is not part of this comparison")
    if not kept:
        raise CorrectionRefused("keep at least one format of this")
    if len(kept) == len(known):
        keep_all(conn, settings, finding_id)
        return ""
    return set_aside(
        conn,
        settings,
        finding_id,
        going=[member.relpath for member in view.members if member.relpath not in kept],
        kept=kept,
        error=CorrectionRefused,
    )


def describe(conn: sqlite3.Connection, item_id: int) -> str:
    """The Quarantine sentence for a format set aside from a work.

    Never "exact duplicate": these files do not share bytes, and saying they do
    would be the one claim this workflow exists to avoid making.
    """
    from librairy.docmeta import cached_facts

    row = conn.execute(
        "SELECT relpath FROM items WHERE id=?", (item_id,)
    ).fetchone()
    if row is None:
        return ""
    facts = cached_facts(conn, item_id, PurePosixPath(str(row["relpath"])))
    if facts is None:
        return ""
    if facts.isbn:
        return "Same ISBN, different file format."
    if facts.doi:
        return "Same DOI, different file format."
    return ""
