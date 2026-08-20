"""The same bytes in two places, and the one decision that answers it.

The audit has been able to see this since the first release and has never been
able to do anything about it. `duplicate` is not in `EXECUTABLE_KINDS`, and the
comment saying why has been right the whole time: *a correct answer exists —
set the copy aside — but that is a different action class with its own safety
semantics, not a move.*

Two things had to be true before it could be offered, and only one of them is
about code.

**LibrAIry must not choose which copy you keep.** Both files are byte-identical,
so there is no measurable difference to appeal to; the difference is what the
folders mean to you, and that is not in the bytes. Every deterministic rule
anyone could write here — keep the deeper one, keep the one sorted first, keep
the one whose folder matches its tags — is a preference wearing a rule's
clothes, and it would be applied to somebody's library at scale. So the row
lists the copies and *you* pick. That is why this is not an `EXECUTABLE_KIND`
and why bulk Approve can never reach it: "approve all confident" has no answer
to "which one".

**And nothing is deleted.** Setting a copy aside moves it to quarantine, which
is a folder you can look in and restore from, exactly like every other
quarantine in LibrAIry. The delete pile is somewhere you empty yourself.

The machinery is entirely borrowed. A quarantine is a plan — see
`quarantine_requests.py` for why that mattered — so this builds one, and the
fingerprint check before execution, the Commit card, the journal entry and Undo
all come with it. Nothing here moves a file.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace

from librairy.config import Settings
from librairy.correction_state import active_plan
from librairy.corrections import CorrectionRefused, load_finding
from librairy.fingerprint import blake2b_file
from librairy.paths import PathValidationError, validate_relpath
from librairy.planner import approve_plan, create_plan, utc_now
from librairy.quarantine import quarantine_operation

KIND = "duplicate"


@dataclass(frozen=True)
class Copy:
    """One of the identical files, and whether it can be the one to go."""

    relpath: str
    size: int
    removable: bool
    reason: str

    @property
    def folder(self) -> str:
        return relpath_parent(self.relpath)


def relpath_parent(relpath: str) -> str:
    return relpath.rpartition("/")[0]


def copies(conn: sqlite3.Connection, settings: Settings, row: sqlite3.Row) -> list[Copy]:
    """Every copy this finding is about, as the index has them *now*.

    Read from `items` by fingerprint rather than from the finding's evidence,
    which is a statement about the moment the audit ran. A copy deleted by hand
    since then should not be offered, and one added since should not be hidden —
    and the count is the whole safety property here, because setting a copy
    aside is only safe while another one exists.
    """
    if row["kind"] != KIND or not row["fingerprint"]:
        return []
    found = [
        Copy(
            relpath=item["relpath"],
            size=int(item["size"] or 0),
            removable=True,
            reason="",
        )
        for item in conn.execute(
            "SELECT relpath, size FROM items"
            " WHERE root='library' AND fingerprint=? AND missing_since IS NULL"
            " ORDER BY relpath",
            (row["fingerprint"],),
        )
    ]
    if len(found) < 2:
        # One copy is not a duplicate. Say so on every row rather than removing
        # them, so the page explains why the buttons are gone.
        return [
            Copy(copy.relpath, copy.size, False, "there is only one copy of this left")
            for copy in found
        ]
    from librairy.protected import protected_roots

    claimed = _claimed(conn, [copy.relpath for copy in found])
    roots = protected_roots(conn)
    return [_judged(settings, copy, claimed, roots) for copy in found]


def _judged(
    settings: Settings, copy: Copy, claimed: set[str], roots: tuple[str, ...]
) -> Copy:
    """Why a copy cannot be the one that goes — said on the row, not implied.

    Every one of these is checked again in `set_aside`. This is what stops the
    page offering a control that would refuse; that is what stops the refusal
    depending on the page.
    """
    from librairy.protected import is_protected

    if copy.relpath in claimed:
        return Copy(copy.relpath, copy.size, False, "already waiting for Commit")
    if is_protected(copy.relpath, roots):
        return Copy(copy.relpath, copy.size, False, "inside a protected folder")
    try:
        path = validate_relpath(settings.library_dir, copy.relpath, kind="finding")
    except PathValidationError:
        return Copy(copy.relpath, copy.size, False, "not a path inside the library")
    if not path.is_file():
        return Copy(copy.relpath, copy.size, False, "no longer on disk")
    return copy


def set_aside(
    conn: sqlite3.Connection, settings: Settings, finding_id: int, relpath: str
) -> str:
    """Move one named copy to quarantine, as one approved plan.

    Every refusal is here and not in the template. The button that is not drawn
    is not the guarantee — the same request arrives from a page left open since
    yesterday, from a second tab, and from curl — and the one that matters most
    is the last-copy check: two people, two tabs, one file each, and quarantine
    would hold both halves of a pair that no longer exists in the library.
    """
    row = load_finding(conn, finding_id)
    if row["kind"] != KIND:
        raise CorrectionRefused("this is not a duplicate")
    if active_plan(conn, finding_id) is not None:
        raise CorrectionRefused("a copy of this is already waiting for Commit")
    available = copies(conn, settings, row)
    chosen = next((copy for copy in available if copy.relpath == relpath), None)
    if chosen is None:
        raise CorrectionRefused("that file is not one of these copies")
    if not chosen.removable:
        raise CorrectionRefused(f"this copy cannot be set aside: {chosen.reason}")
    _assert_unchanged(conn, settings, chosen.relpath)

    #  `quarantine_operation` is the one place the quarantine path shape is
    #  decided — `<date>/<original path>` — and it assumes the inbox, because
    #  that is where every quarantine came from until now. Only the source root
    #  differs here, so it is replaced rather than spelled out again.
    spec = replace(quarantine_operation(chosen.relpath), src_root="library")
    plan_id = create_plan(conn, [spec], settings)
    conn.execute("UPDATE plans SET audit_finding_id=? WHERE id=?", (finding_id, plan_id))
    try:
        # The same race the corrections path documents, for the same reason:
        # `idx_plans_one_active_per_finding` fires on the transition to
        # `approved`, which is the moment a second plan would start claiming a
        # file the first one already claims.
        approve_plan(conn, plan_id, settings)
    except sqlite3.IntegrityError as exc:
        conn.execute("DELETE FROM plan_ops WHERE plan_id=?", (plan_id,))
        conn.execute("DELETE FROM plans WHERE id=?", (plan_id,))
        raise CorrectionRefused(
            "a copy of this was set aside by something else a moment ago"
        ) from exc
    conn.execute(
        "UPDATE audit_findings SET status='accepted', plan_id=?, updated_at=? WHERE id=?",
        (plan_id, utc_now(), finding_id),
    )
    return plan_id


def _assert_unchanged(conn: sqlite3.Connection, settings: Settings, relpath: str) -> None:
    """Still the bytes the index recorded, which is what made it a duplicate."""
    row = conn.execute(
        "SELECT fingerprint FROM items WHERE root='library' AND relpath=?", (relpath,)
    ).fetchone()
    if row is None or not row["fingerprint"]:
        raise CorrectionRefused(
            "this copy has not been indexed, so it cannot be set aside"
        )
    path = validate_relpath(settings.library_dir, relpath, kind="finding")
    if blake2b_file(path) != row["fingerprint"]:
        raise CorrectionRefused("this copy changed since it was last scanned")


def _claimed(conn: sqlite3.Connection, relpaths: list[str]) -> set[str]:
    from librairy.correction_state import ACTIVE_PLAN_STATUSES

    if not relpaths:
        return set()
    slots = ",".join("?" * len(relpaths))
    statuses = ",".join("?" * len(ACTIVE_PLAN_STATUSES))
    return {
        found["src_relpath"]
        for found in conn.execute(
            f"SELECT o.src_relpath FROM plan_ops o JOIN plans p ON p.id = o.plan_id"  # noqa: S608
            f" WHERE o.src_root='library' AND o.src_relpath IN ({slots})"
            f" AND p.status IN ({statuses})",
            (*relpaths, *ACTIVE_PLAN_STATUSES),
        )
    }
