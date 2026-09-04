"""Three words that are not synonyms, and the one thing none of them may do.

One remote, the whole library, copy-only — three different intentions collapsed
into one setting. This is where they come apart, and it is written down before
any transfer command exists, because an innocent `rclone sync` violates the
whole product in a single argument.

## The Library is authoritative. Always.

    Library  ────────────►  destination

and never the other way. A destination is an outward copy of Library content.
Nothing found at a destination is ever an instruction to add, change or remove
anything in the Library — not a newer file, not a different file, not a file
the Library has never heard of. **This is not two-way sync**, and the absence of
an inward arrow is the whole safety model.

## The three modes

    Backup          recovery and retained copies. A file leaving the Library
                    is not a reason to remove it from a backup — that is what a
                    backup is *for*. Safety beats equivalence.

    Mirror          a place that should represent the *current* Library.
                    Additions and changes go out. Divergence is **reported**:
                    "37 files here are no longer in your Library", and you
                    decide.

    Offline Backup  a registered drive that may be unplugged for months. Its
                    actions exist only while it is attached. On reconnect: what
                    to add, what to update, and what is there that the Library
                    no longer has — reported, never removed.

**Mirror is the one worth being careful about.** Mirror means LibrAIry knows
the destination differs from the Library. It does not mean LibrAIry has
permission to erase the difference. That costs some "perfect sync" purity and
it is the right trade for a program whose entire premise is that it does not
delete your files. If deletion is ever offered it will be its own reviewed
workflow with a preview, and it will not arrive by widening the meaning of a
word somebody already chose.

## Deletion is not a thing this module can express

Look at `ACTIONS` below. It is the full cross product of every mode and every
way a destination can differ from the Library, and there are four possible
answers: copy it, update it, leave it alone, or tell somebody. There is no
delete constant to put in a cell, so no future edit can quietly add one to a
table — it would have to invent the concept first, in a module whose docstring
is this one. `tests/test_destinations.py` holds that.

## What LibrAIry owns, and what rclone owns

    LibrAIry   policy, planning, comparison, status, scheduling, safety,
               history, and every word of the interface
    rclone     moving the bytes

Reimplementing a transfer engine would be foolish. Handing authority to
rclone's defaults would be worse: the command is generated from a plan, verb by
verb, and `tools/rclone.py` refuses anything that could remove data at either
end.

## The policy model

    Category → Destination → Mode

`Photos → NAS Backup → Backup`. Deliberately not a rules language: folder and
subtree rules can be added later on top of this, and building the editor first
is how a feature nobody can explain gets shipped.

**Overlap is deterministic by construction.** One category may go to several
destinations — that is fan-out and it is the point — but a category and a
destination together have exactly one mode, held by a unique key rather than by
precedence arithmetic. Two policies that disagreed about what a destination is
*for* would be the one ambiguity nobody could resolve by reading the screen.

See `docs/ROADMAP.md` M3-03.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from librairy.planner import utc_now
from librairy.taxonomy import CATEGORIES

#  The three modes. The value is what goes in the database and what appears in
#  a URL, so it is stable; the label is what a person reads.
BACKUP = "backup"
MIRROR = "mirror"
OFFLINE = "offline"

MODES = (BACKUP, MIRROR, OFFLINE)

MODE_LABEL = {
    BACKUP: "Backup",
    MIRROR: "Mirror",
    OFFLINE: "Offline Backup",
}

#  One sentence each, in the words `docs/ui-vocabulary.md` pins. These are
#  shown wherever a mode is chosen, because "backup" and "mirror" mean whatever
#  the last program somebody used meant by them.
MODE_MEANING = {
    BACKUP: "Keeps recovery copies. Removing a file from your library never removes it here.",
    MIRROR: "Keeps this destination current. Anything here that your library no longer"
    " has is reported to you, never deleted.",
    OFFLINE: "Updates while the registered drive is connected. Nothing is ever removed"
    " from it.",
}

#  How a destination can differ from the Library. Four states and no fifth:
#  every comparison this program makes lands in exactly one of them.
MISSING = "missing"  # in the Library, not at the destination
CHANGED = "changed"  # at both, and the destination copy is not the same bytes
CURRENT = "current"  # at both, and the same
EXTRA = "extra"  # at the destination, and not in the Library

DIFFERENCES = (MISSING, CHANGED, CURRENT, EXTRA)

DIFFERENCE_LABEL = {
    MISSING: "not backed up yet",
    CHANGED: "changed since it was copied",
    CURRENT: "up to date",
    EXTRA: "here, but no longer in your library",
}

#  What may be done about a difference. Four verbs.
#
#  **There is no fifth.** No `DELETE`, no `REMOVE`, no `PURGE` — not as a
#  constant, not as a branch, not as an option behind a setting. A future edit
#  that wanted a destination file gone would have to invent the concept in this
#  module first, which is a conversation rather than a typo. That is the
#  difference between a rule and a convention.
COPY = "copy"
UPDATE = "update"
KEEP = "keep"
REPORT = "report"

ACTIONS: dict[tuple[str, str], str] = {
    #  Backup: everything goes out, nothing comes back, and what is already
    #  there stays there. A file that left the Library is exactly what a backup
    #  exists to still have.
    (BACKUP, MISSING): COPY,
    (BACKUP, CHANGED): UPDATE,
    (BACKUP, CURRENT): KEEP,
    (BACKUP, EXTRA): KEEP,
    #  Mirror: the same outward behaviour, and one difference — it *says* when
    #  the destination holds something the Library does not. Saying so is the
    #  whole of what "mirror" adds. It is not permission.
    (MIRROR, MISSING): COPY,
    (MIRROR, CHANGED): UPDATE,
    (MIRROR, CURRENT): KEEP,
    (MIRROR, EXTRA): REPORT,
    #  Offline: identical, and reported rather than kept quiet, because a drive
    #  that has been in a drawer for three months is precisely where somebody
    #  wants to be told what has drifted.
    (OFFLINE, MISSING): COPY,
    (OFFLINE, CHANGED): UPDATE,
    (OFFLINE, CURRENT): KEEP,
    (OFFLINE, EXTRA): REPORT,
}

#  Actions that put bytes on a destination. Used to size a plan and to decide
#  whether there is anything to do at all.
TRANSFERS = (COPY, UPDATE)

#  What a destination is reached through.
LOCAL = "local"  # a path on this machine, including a mounted drive
REMOTE = "remote"  # an rclone remote

KINDS = (LOCAL, REMOTE)


@dataclass(frozen=True)
class Destination:
    """Somewhere Library content is copied to."""

    id: int
    name: str
    kind: str
    #  An rclone remote (`nas:library`) or an absolute local path. Never used
    #  as transfer authority on its own — see `librairy/transfer_paths.py`.
    target: str
    #  Which modes this destination may be used in. A drive that is normally
    #  unplugged can only be an Offline Backup; a NAS can be either of the
    #  other two.
    modes: tuple[str, ...]
    enabled: bool
    #  For an offline drive: something that identifies the volume itself rather
    #  than wherever the operating system mounted it this morning. Checked
    #  before a single byte is written, because `/Volumes/Backup` is whatever
    #  was plugged in most recently.
    identity: str = ""
    created_at: str = ""

    @property
    def offline_only(self) -> bool:
        return tuple(self.modes) == (OFFLINE,)


@dataclass(frozen=True)
class Policy:
    """One category, going to one destination, in one mode."""

    id: int
    category: str
    destination_id: int
    mode: str
    enabled: bool
    created_at: str = ""


def action_for(mode: str, difference: str) -> str:
    """What this mode does about this difference. The whole decision table."""
    if mode not in MODES:
        raise ValueError(f"unknown mode: {mode}")
    if difference not in DIFFERENCES:
        raise ValueError(f"unknown difference: {difference}")
    return ACTIONS[(mode, difference)]


# --- the store ---------------------------------------------------------------------


def add_destination(
    conn: sqlite3.Connection,
    *,
    name: str,
    kind: str,
    target: str,
    modes: tuple[str, ...] | list[str],
    identity: str = "",
    enabled: bool = True,
) -> int:
    if kind not in KINDS:
        raise ValueError(f"unknown destination kind: {kind}")
    allowed = tuple(mode for mode in MODES if mode in modes)
    if not allowed:
        raise ValueError("a destination has to be usable in at least one mode")
    if not name.strip() or not target.strip():
        raise ValueError("a destination needs a name and a target")
    cursor = conn.execute(
        """
        INSERT INTO backup_destinations(name, kind, target, modes, identity, enabled,
                                        created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            name.strip(),
            kind,
            target.strip(),
            ",".join(allowed),
            identity.strip(),
            int(enabled),
            utc_now(),
        ),
    )
    return int(cursor.lastrowid)


def destinations(conn: sqlite3.Connection, *, enabled_only: bool = False) -> list[Destination]:
    where = " WHERE enabled = 1" if enabled_only else ""
    return [
        _destination(row)
        for row in conn.execute(
            f"SELECT * FROM backup_destinations{where} ORDER BY name COLLATE NOCASE"  # noqa: S608
        )
    ]


def destination(conn: sqlite3.Connection, destination_id: int) -> Destination | None:
    row = conn.execute(
        "SELECT * FROM backup_destinations WHERE id=?", (destination_id,)
    ).fetchone()
    return _destination(row) if row is not None else None


def set_enabled(conn: sqlite3.Connection, destination_id: int, enabled: bool) -> None:
    """Switch a destination off. Nothing is removed anywhere; work simply stops."""
    conn.execute(
        "UPDATE backup_destinations SET enabled=? WHERE id=?",
        (int(enabled), destination_id),
    )


def remove_destination(conn: sqlite3.Connection, destination_id: int) -> None:
    """Forget a destination. **Its files are not touched.**

    Deleting the row deletes what LibrAIry knows about the place, and nothing
    at the place itself — which is the same promise every other mode makes and
    the reason this is safe to offer at all.
    """
    conn.execute("DELETE FROM backup_policies WHERE destination_id=?", (destination_id,))
    conn.execute("DELETE FROM backup_destinations WHERE id=?", (destination_id,))


def set_policy(
    conn: sqlite3.Connection,
    *,
    category: str,
    destination_id: int,
    mode: str,
    enabled: bool = True,
) -> int:
    """Send one category to one destination in one mode.

    A category and a destination have exactly one mode between them, held by a
    unique key. Two policies disagreeing about what a destination is *for*
    would be the one ambiguity nobody could resolve by reading the screen — so
    setting it again replaces it rather than adding a second opinion.
    """
    if category not in CATEGORIES:
        raise ValueError(f"unknown category: {category}")
    if mode not in MODES:
        raise ValueError(f"unknown mode: {mode}")
    found = destination(conn, destination_id)
    if found is None:
        raise ValueError("that destination does not exist")
    if mode not in found.modes:
        #  A drive that lives in a drawer cannot be a Mirror: a mode it can
        #  never satisfy would produce a policy that is permanently failing,
        #  and a permanently failing policy teaches people to ignore the page
        #  that reports it.
        raise ValueError(f"{found.name} cannot be used as {MODE_LABEL[mode]}")
    conn.execute(
        """
        INSERT INTO backup_policies(category, destination_id, mode, enabled, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(category, destination_id) DO UPDATE SET
          mode=excluded.mode, enabled=excluded.enabled
        """,
        (category, destination_id, mode, int(enabled), utc_now()),
    )
    row = conn.execute(
        "SELECT id FROM backup_policies WHERE category=? AND destination_id=?",
        (category, destination_id),
    ).fetchone()
    return int(row["id"])


def clear_policy(conn: sqlite3.Connection, *, category: str, destination_id: int) -> None:
    conn.execute(
        "DELETE FROM backup_policies WHERE category=? AND destination_id=?",
        (category, destination_id),
    )


def policies(conn: sqlite3.Connection, *, enabled_only: bool = False) -> list[Policy]:
    where = " WHERE enabled = 1" if enabled_only else ""
    return [
        Policy(
            id=int(row["id"]),
            category=str(row["category"]),
            destination_id=int(row["destination_id"]),
            mode=str(row["mode"]),
            enabled=bool(row["enabled"]),
            created_at=str(row["created_at"] or ""),
        )
        for row in conn.execute(
            f"SELECT * FROM backup_policies{where} ORDER BY category, destination_id"  # noqa: S608
        )
    ]


def active(conn: sqlite3.Connection) -> list[tuple[Policy, Destination]]:
    """Every policy that could actually run, with the destination it names.

    Both halves have to be switched on. A policy pointing at a disabled
    destination is not an error and not a warning — it is somebody having
    turned the destination off, which is the whole reason that switch exists.
    """
    known = {found.id: found for found in destinations(conn)}
    return [
        (policy, known[policy.destination_id])
        for policy in policies(conn, enabled_only=True)
        if policy.destination_id in known and known[policy.destination_id].enabled
    ]


def _destination(row: sqlite3.Row) -> Destination:
    return Destination(
        id=int(row["id"]),
        name=str(row["name"]),
        kind=str(row["kind"]),
        target=str(row["target"]),
        modes=tuple(part for part in str(row["modes"]).split(",") if part),
        enabled=bool(row["enabled"]),
        identity=str(row["identity"] or ""),
        created_at=str(row["created_at"] or ""),
    )
