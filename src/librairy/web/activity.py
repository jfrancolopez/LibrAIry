"""What the worker is doing right now, for the header pill on every page.

Dropping files into the inbox and then wondering whether anything noticed is a
bad feeling, and the dashboard only answers it if you happen to be looking at
the dashboard. This is the small, cheap answer that rides along on every page.

Cheap matters: it is polled every few seconds from every open tab, so it is
three indexed counts and a couple of key reads, and nothing that touches the
filesystem or the network.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

# Phases the worker reports while it is actually doing something.
WORKING_PHASES = {"scan", "dedup", "analyze", "content", "backup"}
PHASE_LABELS = {
    "scan": "scanning the inbox",
    "dedup": "checking for duplicates",
    "analyze": "identifying files",
    "content": "reading documents",
    "backup": "backing up",
}
# Past this, the worker is not running rather than merely quiet.
STALE_HEARTBEAT_SECONDS = 180


@dataclass(frozen=True)
class Activity:
    phase: str
    label: str
    #  Inbox files found but not yet identified.
    queued: int
    #  Proposals waiting for a human.
    awaiting_review: int
    busy: bool
    stalled: bool
    last_cycle_at: str

    @property
    def visible(self) -> bool:
        """Nothing to say when the worker is healthy and there is no backlog."""
        return self.busy or self.stalled or bool(self.queued)


def activity(conn: sqlite3.Connection) -> Activity:
    state = _worker_state(conn)
    phase = str(state.get("current_phase") or "unknown")
    last_cycle = str(state.get("last_cycle_at") or "")
    queued = _count(conn, "SELECT COUNT(*) FROM items WHERE root='inbox' AND state='discovered'")
    awaiting = _count(conn, "SELECT COUNT(*) FROM proposals WHERE status='proposed'")
    busy = phase in WORKING_PHASES
    return Activity(
        phase=phase,
        label=PHASE_LABELS.get(phase, phase),
        queued=queued,
        awaiting_review=awaiting,
        busy=busy,
        stalled=_stalled(last_cycle),
        last_cycle_at=last_cycle,
    )


def _stalled(last_cycle_at: str) -> bool:
    """A backlog that nobody is working through is worth saying out loud."""
    if not last_cycle_at:
        return False
    try:
        seen = datetime.fromisoformat(last_cycle_at)
    except ValueError:
        return False
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=UTC)
    return (datetime.now(UTC) - seen).total_seconds() > STALE_HEARTBEAT_SECONDS


def _worker_state(conn: sqlite3.Connection) -> dict[str, object]:
    state: dict[str, object] = {}
    for row in conn.execute("SELECT key, value FROM worker_state"):
        try:
            state[row["key"]] = json.loads(row["value"])
        except (TypeError, json.JSONDecodeError):
            state[row["key"]] = row["value"]
    return state


def _count(conn: sqlite3.Connection, sql: str) -> int:
    row = conn.execute(sql).fetchone()
    return int(row[0]) if row else 0
