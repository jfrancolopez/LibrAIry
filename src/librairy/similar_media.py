"""Two files that are the same recording and not the same bytes.

    Music/Queen/A Night at the Opera/01 - Death on Two Legs.flac    28.4 MB
    Music/Queen/A Night at the Opera/01 - Death on Two Legs.mp3      4.9 MB

No hash pairs these and no hash ever will. czkawka does — it has been pairing
them into `similar_media_flags` since the first release — and nothing has ever
been able to act on what it found. The comparison panel could show two files
side by side in the inbox; for two files already filed, the finding did not
exist.

**This is a comparison, not a verdict.** The whole difficulty is that "which
one do you want" has no technical answer:

    lossless is bigger              and you may be filling a phone
    HEVC is newer                   and your TV may not decode it
    4K is more pixels               and it may be an upscale
    the newer file is newer         and may be the worse rip

So the row lays out what was measured — container, codec, resolution, bitrate,
duration, size — and stops. Nothing here writes `best`, `recommended` or
`higher quality`, and nothing sorts the members so that one of them is
implicitly the answer. Facts, in the order they were measured, and the person
decides.

Three separate ideas, kept separate, because collapsing them is how a
comparison tool turns into a deletion assistant:

    exact duplicate         identical bytes — `audit_duplicates.py`
    similar representation  the same recording, encoded differently — here
    related media           the official video and the live one; a studio
                            take and a concert take. **Not duplicates**, and
                            never treated as such: nothing in this module
                            groups anything by title, artist or tags. The
                            groups come from czkawka's own pairs and from
                            nowhere else, which is what stops a matching
                            title from ever being enough.

What happens after the choice is borrowed whole. Keeping one of four is one
decision producing three quarantine operations in one plan — one Commit card,
one journal entry, one Undo. Keeping *all* of them is not a plan at all: there
is no filesystem work in "leave things as they are", and inventing an empty
plan so the workflow looked uniform would put a no-op in Commit, in History and
in Undo. It dismisses the czkawka pairs instead, which is also what stops the
next audit asking the same question again.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from pathlib import PurePosixPath

from librairy.config import Settings
from librairy.fingerprint import blake2b_file
from librairy.paths import PathValidationError, validate_relpath
from librairy.planner import approve_plan, create_plan, utc_now
from librairy.quarantine import quarantine_operation

KIND = "similar-media"

# A group bigger than this is not a comparison any more. Four encodes of one
# song is a decision; forty near-identical frames from a burst is a different
# feature, and reading forty technical tables to answer one question is not
# something a row should ask anybody to do.
MAX_MEMBERS = 8

# What czkawka was comparing when it paired them. Its `duplicate` mode finds
# identical bytes, which is the exact-duplicate workflow's question and not
# this one.
SIMILAR_KINDS = ("image", "video", "audio")


@dataclass(frozen=True)
class Member:
    """One representation, and what was measured about it."""

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


@dataclass(frozen=True)
class Comparison:
    """One similar-media finding: the members, and the facts that differ."""

    finding_id: int
    members: tuple[Member, ...]

    @property
    def labels(self) -> tuple[str, ...]:
        """Every measured property, in the order the first member gave them."""
        seen: list[str] = []
        for member in self.members:
            for label, _ in member.facts:
                if label not in seen:
                    seen.append(label)
        return tuple(seen)

    def values(self, label: str) -> tuple[str, ...]:
        return tuple(dict(member.facts).get(label, "—") for member in self.members)

    @property
    def differences(self) -> tuple[str, ...]:
        return tuple(
            label for label in self.labels if len(set(self.values(label))) > 1
        )

    @property
    def resolvable(self) -> bool:
        return len(self.members) >= 2


def _pair_key(left: str = "a", right: str = "b") -> str:
    """The two fingerprints of a pair, in a fixed order, as one SQL string."""
    return (
        f"(CASE WHEN {left}.fingerprint < {right}.fingerprint"
        f" THEN {left}.fingerprint || '|' || {right}.fingerprint"
        f" ELSE {right}.fingerprint || '|' || {left}.fingerprint END)"
    )


def active_clause(left: str = "a", right: str = "b", flag: str = "f") -> str:
    """Is this pair still a live question?

    Two ways to be live. Never answered, or answered about two files that are
    no longer these two — because "keep both" was a statement about the bytes
    somebody was looking at, and a re-encode since then is a comparison nobody
    has been asked about.

    A dismissal that recorded no fingerprints predates this and stays
    dismissed. NULL means "answered the old way", not "answered about nothing".
    """
    return (
        f"({flag}.status='review' OR ({flag}.dismissed_fingerprints IS NOT NULL"
        f" AND {flag}.dismissed_fingerprints <> {_pair_key(left, right)}))"
    )


def dismiss_between(conn: sqlite3.Connection, item_ids: list[int]) -> int:
    """Answer every pair among these items with "keep them all".

    Records the fingerprints as well as the answer, so the suppression is about
    two specific files rather than about two rows that will still be there
    after somebody replaces one of them.
    """
    if len(item_ids) < 2:
        return 0
    slots = ",".join("?" * len(item_ids))
    cursor = conn.execute(
        f"""
        UPDATE similar_media_flags
        SET status='dismissed',
            dismissed_fingerprints = (
              SELECT CASE WHEN a.fingerprint < b.fingerprint
                          THEN a.fingerprint || '|' || b.fingerprint
                          ELSE b.fingerprint || '|' || a.fingerprint END
              FROM items a, items b
              WHERE a.id = similar_media_flags.item_id
                AND b.id = similar_media_flags.similar_item_id
            )
        WHERE item_id IN ({slots}) AND similar_item_id IN ({slots})
        """,  # noqa: S608 - the placeholders are counted from the argument
        (*item_ids, *item_ids),
    )
    return cursor.rowcount


# --- finding the groups -------------------------------------------------------------


def detect(conn: sqlite3.Connection) -> list:
    """One finding per group of library files czkawka paired with each other.

    Pairs are what the table stores, and a group is what a person answers, so
    the pairs are joined into connected components here. Three encodes of one
    song usually arrive as three pairs and are one question.

    Every filter is a refusal to widen the claim. `dismissed` pairs are the
    ones somebody already decided to keep both of. Byte-identical pairs belong
    to the exact-duplicate workflow, which knows what rmlint said and can say
    so. And nothing is grouped by name, tags or catalog — if czkawka did not
    pair two files, they are not in a group, however alike they look.
    """
    from librairy.audit import Finding
    from librairy.models import EvidenceEntry

    edges = conn.execute(
        f"""
        SELECT a.id AS a_id, a.relpath AS a_path, a.fingerprint AS a_fp,
               b.id AS b_id, b.relpath AS b_path, b.fingerprint AS b_fp,
               f.kind AS kind, f.score AS score
        FROM similar_media_flags f
        JOIN items a ON a.id = f.item_id
        JOIN items b ON b.id = f.similar_item_id
        WHERE {active_clause()}
          AND f.kind IN ({",".join("?" * len(SIMILAR_KINDS))})
          AND a.root = 'library' AND b.root = 'library'
          AND a.missing_since IS NULL AND b.missing_since IS NULL
        """,  # noqa: S608 - the placeholders are the constant above
        SIMILAR_KINDS,
    ).fetchall()

    groups: dict[int, set[int]] = {}
    paths: dict[int, str] = {}
    scores: dict[int, float | None] = {}
    kinds: dict[int, str] = {}
    for edge in edges:
        if edge["a_fp"] and edge["a_fp"] == edge["b_fp"]:
            continue
        left, right = int(edge["a_id"]), int(edge["b_id"])
        paths[left], paths[right] = str(edge["a_path"]), str(edge["b_path"])
        merged = groups.get(left, {left}) | groups.get(right, {right})
        for member in merged:
            groups[member] = merged
        anchor = min(merged)
        scores[anchor] = edge["score"]
        kinds[anchor] = str(edge["kind"])

    findings = []
    for members in {id(group): group for group in groups.values()}.values():
        if len(members) > MAX_MEMBERS:
            continue
        ordered = sorted(members, key=lambda item: paths[item])
        anchor = min(members)
        keep, *rest = ordered
        findings.append(
            Finding(
                relpath=paths[keep],
                kind=KIND,
                severity="review",
                summary=(
                    f"Looks like the same {kinds.get(anchor, 'media')} as "
                    f"{len(rest)} other file(s), encoded differently."
                ),
                evidence=[
                    EvidenceEntry(
                        "czkawka",
                        "paired by appearance",
                        f"{scores[anchor]:.2f}"
                        if isinstance(scores.get(anchor), int | float)
                        else "similar",
                        0.7,
                    ),
                    *[
                        EvidenceEntry("filesystem", "compared with", paths[item], 0.9)
                        for item in rest
                    ],
                ],
            )
        )
    return findings


def is_similar_finding(row: sqlite3.Row) -> bool:
    try:
        return row["kind"] == KIND
    except (KeyError, IndexError):
        return False


# --- reading the comparison ---------------------------------------------------------


def compare(
    conn: sqlite3.Connection,
    settings: Settings,
    row: sqlite3.Row,
    *,
    measure: bool = True,
) -> Comparison | None:
    """The members as the index has them now, with what was measured about each.

    Read from the flags rather than from the finding's evidence, which is a
    statement about the moment the audit ran. A representation deleted by hand
    since then is not something to choose between.

    `measure=False` skips the media tools. A list page drawing forty rows must
    not run eighty ffprobes to do it; the numbers appear when the comparison is
    opened, which is the only moment anybody reads them.
    """
    if not is_similar_finding(row):
        return None
    members = _members(conn, settings, row)
    if len(members) < 2:
        return None
    if measure:
        members = [
            replace(member, facts=technical_facts(settings, member.relpath))
            for member in members
        ]
    return Comparison(finding_id=int(row["id"]), members=tuple(members))


def _members(
    conn: sqlite3.Connection, settings: Settings, row: sqlite3.Row
) -> list[Member]:
    anchor = conn.execute(
        "SELECT id FROM items WHERE root='library' AND relpath=? AND missing_since IS NULL",
        (row["relpath"],),
    ).fetchone()
    if anchor is None:
        return []
    ids = _connected(conn, int(anchor["id"]))
    found: list[Member] = []
    for item_id in ids:
        item = conn.execute(
            "SELECT id, relpath, size FROM items"
            " WHERE id=? AND root='library' AND missing_since IS NULL",
            (item_id,),
        ).fetchone()
        if item is None:
            continue
        try:
            path = validate_relpath(settings.library_dir, str(item["relpath"]), kind="finding")
        except PathValidationError:
            continue
        if not path.is_file():
            continue
        found.append(
            Member(
                item_id=int(item["id"]),
                relpath=str(item["relpath"]),
                size=int(item["size"] or 0),
            )
        )
    return sorted(found, key=lambda member: member.relpath)


def _connected(conn: sqlite3.Connection, item_id: int) -> list[int]:
    """Everything reachable from this item through `review` pairs."""
    seen = {item_id}
    frontier = [item_id]
    while frontier:
        current = frontier.pop()
        for row in conn.execute(
            f"""
            SELECT CASE WHEN f.item_id=? THEN f.similar_item_id ELSE f.item_id END AS other
            FROM similar_media_flags f
            JOIN items a ON a.id = f.item_id
            JOIN items b ON b.id = f.similar_item_id
            WHERE {active_clause()} AND f.kind IN ({",".join("?" * len(SIMILAR_KINDS))})
              AND (f.item_id=? OR f.similar_item_id=?)
            """,  # noqa: S608 - the placeholders are the constant above
            (current, *SIMILAR_KINDS, current, current),
        ):
            other = int(row["other"])
            if other not in seen:
                seen.add(other)
                frontier.append(other)
    return sorted(seen)


def technical_facts(
    settings: Settings, relpath: str, *, root: str = "library"
) -> tuple[tuple[str, str], ...]:
    """Container, codec, resolution, bitrate, duration — measured, not judged.

    The same readers the inbox comparison panel has always used, so a FLAC is
    described here exactly as it is described there. Nothing is fetched: no
    catalog, no network, no model. A comparison that had to ask the internet
    what a file is would be a comparison you could not run offline, which is
    most of the point of this program.
    """
    from librairy.duplicates import _read_image, _read_media
    from librairy.humanize import human_bytes
    from librairy.mediakind import kind_for

    path = (settings.inbox_dir if root == "inbox" else settings.library_dir) / relpath
    facts: list[tuple[str, str]] = [("Size", human_bytes(_size(path)))]
    kind = kind_for(path)
    if kind in {"audio", "video", "image"}:
        reader = _read_image if kind == "image" else _read_media
        try:
            measured = reader(path, settings)
        except Exception:  # noqa: BLE001 - a missing binary means "no fact"
            measured = {}
        facts.extend(measured.items())
    return tuple(facts)


def _size(path) -> int:  # noqa: ANN001
    try:
        return path.stat().st_size
    except OSError:
        return 0


# --- the decision -------------------------------------------------------------------


def resolve(
    conn: sqlite3.Connection,
    settings: Settings,
    finding_id: int,
    keep: list[str],
) -> str:
    """Keep the named representations; the rest wait for Commit, then Quarantine.

    Returns the plan id, or an empty string when keeping everything — which is
    a real outcome and not a plan. Nothing is deleted in either case.

    The refusal that matters is the first one. Quarantining every member would
    empty the library of a recording somebody has, in the name of tidying up
    the fact that they have two of it.
    """
    from librairy.correction_state import active_plan
    from librairy.corrections import CorrectionRefused, load_finding

    row = load_finding(conn, finding_id)
    if not is_similar_finding(row):
        raise CorrectionRefused("this is not a comparison")
    if active_plan(conn, finding_id) is not None:
        raise CorrectionRefused("this comparison is already waiting for Commit")
    view = compare(conn, settings, row, measure=False)
    if view is None:
        raise CorrectionRefused("there is only one of these left")
    known = {member.relpath for member in view.members}
    kept = [relpath for relpath in dict.fromkeys(keep) if relpath in known]
    if len(kept) != len(set(keep)):
        raise CorrectionRefused("one of those files is not part of this comparison")
    if not kept:
        raise CorrectionRefused("keep at least one of these")
    if len(kept) == len(known):
        return _keep_all(conn, view, finding_id)

    going = [member for member in view.members if member.relpath not in kept]
    for member in going:
        _assert_unchanged(conn, settings, member.relpath)
    for relpath in kept:
        _assert_unchanged(conn, settings, relpath)
    specs = [
        replace(quarantine_operation(member.relpath), src_root="library")
        for member in going
    ]
    plan_id = create_plan(conn, specs, settings)
    conn.execute("UPDATE plans SET audit_finding_id=? WHERE id=?", (finding_id, plan_id))
    try:
        approve_plan(conn, plan_id, settings)
    except sqlite3.IntegrityError as exc:
        conn.execute("DELETE FROM plan_ops WHERE plan_id=?", (plan_id,))
        conn.execute("DELETE FROM plans WHERE id=?", (plan_id,))
        raise CorrectionRefused(
            "this comparison was answered by something else a moment ago"
        ) from exc
    conn.execute(
        "UPDATE audit_findings SET status='accepted', plan_id=?, updated_at=? WHERE id=?",
        (plan_id, utc_now(), finding_id),
    )
    return plan_id


def _keep_all(conn: sqlite3.Connection, view: Comparison, finding_id: int) -> str:
    """"Leave them all alone" — answered, with no filesystem work in it.

    There is nothing to commit, nothing to journal and nothing to undo, so no
    plan is made. What does have to happen is that the next audit does not ask
    again: the czkawka pairs behind this group are marked `dismissed`, which is
    the suppression the flags table has always had a column for. The finding
    itself is marked `kept` too, but that alone would not hold — re-running the
    audit rewrites a finding's status to `open`, so suppressing the *evidence*
    is what makes the answer stick.
    """
    dismiss_between(conn, [member.item_id for member in view.members])
    conn.execute(
        "UPDATE audit_findings SET status='kept', updated_at=? WHERE id=?",
        (utc_now(), finding_id),
    )
    return ""


def _assert_unchanged(
    conn: sqlite3.Connection, settings: Settings, relpath: str
) -> None:
    from librairy.corrections import CorrectionRefused

    row = conn.execute(
        "SELECT fingerprint FROM items WHERE root='library' AND relpath=?", (relpath,)
    ).fetchone()
    if row is None or not row["fingerprint"]:
        raise CorrectionRefused(f"{PurePosixPath(relpath).name} has not been indexed")
    try:
        path = validate_relpath(settings.library_dir, relpath, kind="finding")
    except PathValidationError as exc:
        raise CorrectionRefused(f"{PurePosixPath(relpath).name} is not a library path") from exc
    if not path.is_file() or blake2b_file(path) != row["fingerprint"]:
        raise CorrectionRefused(
            f"{PurePosixPath(relpath).name} changed since these were compared"
        )


# --- what the rest of the program asks ----------------------------------------------


def kept_members(
    conn: sqlite3.Connection, plan_id: str, rows: list[sqlite3.Row]
) -> list[str]:
    """The representations this plan is *not* setting aside.

    The whole safety property of a comparison is that something survives it.
    The plan holds only what goes, so what stays is the group minus the plan —
    which is why this is computed rather than stored: a member restored or
    re-added between approval and Commit is part of the answer either way.
    """
    finding = conn.execute(
        "SELECT f.id, f.kind, f.relpath FROM plans p"
        " JOIN audit_findings f ON f.id = p.audit_finding_id WHERE p.id=?",
        (plan_id,),
    ).fetchone()
    if finding is None or finding["kind"] != KIND:
        return []
    going = {
        str(row["src_relpath"]) for row in rows if row["src_root"] == "library"
    }
    anchor = conn.execute(
        "SELECT id FROM items WHERE root='library' AND relpath=?",
        (finding["relpath"],),
    ).fetchone()
    if anchor is None:
        return []
    members = [
        str(row["relpath"])
        for item_id in _connected(conn, int(anchor["id"]))
        for row in conn.execute(
            "SELECT relpath FROM items WHERE id=? AND root='library'", (item_id,)
        )
    ]
    return sorted(relpath for relpath in members if relpath not in going)


def companion_of(conn: sqlite3.Connection, item_id: int) -> int | None:
    """A library member of the same comparison, for the Quarantine row to name.

    "Set aside after comparing it with…" needs something to point at, and the
    honest answer is one of the representations that stayed. Never called an
    exact duplicate: these files do not have the same bytes, and saying they do
    would be the one claim this workflow exists to avoid making.
    """
    for other in _connected(conn, item_id):
        if other == item_id:
            continue
        row = conn.execute(
            "SELECT id FROM items WHERE id=? AND root='library' AND missing_since IS NULL",
            (other,),
        ).fetchone()
        if row is not None:
            return int(row["id"])
    return None
