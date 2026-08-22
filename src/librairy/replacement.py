"""One representation taking another's place, written down the same way every time.

Three workflows arrive at the identical decision from three directions:

    inbox      -> library    an arriving version wins the comparison
    quarantine -> library    a held version is put back in the filed one's place
    library    -> library    two filed versions, and one becomes the active one

They are the same two operations in the same order, and the order is the whole
safety argument: **preserve first, admit second**. The version being displaced
goes to Quarantine before anything moves onto its slot, so at no point does the
library hold neither — and there is no overwrite anywhere in it, because
LibrAIry has no operation that loses bytes.

The plan is marked `coherent`, which is what tells the executor these two are
one decision: both run or neither does, revalidated as a unit before the first
byte moves. That column exists precisely so this property does not have to be
inferred from which feature happened to build the plan.

This module is the shared spelling of it — not a new executor primitive, and
not a fourth workflow. Each caller still decides *what* may be replaced and
*where* the slot is, which is the part that differs and the part that carries
the product judgement.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace

from librairy.config import Settings
from librairy.planner import OperationSpec, approve_plan, create_plan
from librairy.quarantine import quarantine_operation


def swap_specs(
    *, preserve: str, source_root: str, source_relpath: str, dest_relpath: str
) -> list[OperationSpec]:
    """Preserve the filed version, then move the chosen one into its slot.

    In this order and only this order. Reversed, a failure halfway leaves the
    library with the new file in place and the old one still filed under a name
    that no longer describes it; worse, on the same-path case the move would
    have nowhere to land.
    """
    return [
        replace(quarantine_operation(preserve), src_root="library"),
        OperationSpec(
            op_type="move",
            src_root=source_root,
            src_relpath=source_relpath,
            dest_root="library",
            dest_relpath=dest_relpath,
        ),
    ]


def approve_coherent(
    conn: sqlite3.Connection,
    settings: Settings,
    specs: list[OperationSpec],
    *,
    error: type[Exception],
    clash: str = "one of these files is already waiting for Commit",
    entry_id: int | None = None,
) -> str:
    """Create the plan, mark it coherent, approve it. Cleans up if it clashes.

    `error` is the caller's own exception type, so a refusal reads in the
    vocabulary of the page it came from: a Review correction and a Quarantine
    decision report failures through different doors.

    `entry_id` is written **before** approval, not after. The unique index that
    stops two live decisions about one held file is checked when the plan is
    approved, so a link added afterwards is a link the database never got to
    enforce.
    """
    plan_id = create_plan(conn, specs, settings)
    conn.execute(
        "UPDATE plans SET coherent=1, quarantine_entry_id=COALESCE(?, quarantine_entry_id)"
        " WHERE id=?",
        (entry_id, plan_id),
    )
    try:
        approve_plan(conn, plan_id, settings)
    except sqlite3.IntegrityError as exc:
        conn.execute("DELETE FROM plan_ops WHERE plan_id=?", (plan_id,))
        conn.execute("DELETE FROM plans WHERE id=?", (plan_id,))
        raise error(clash) from exc
    return plan_id
