"""What the backup system looks like from outside, for everything that shows it.

One read model, four surfaces — Settings, Health, the Dashboard and the
transfer history — because a destination that reads as healthy on one page and
failed on another is worse than either answer alone. Nothing here writes,
plans, transfers, or decides; every number is read from what the machinery
already recorded.

It lives beside the other transfer modules rather than under `web/` because
Health reads it too, and a domain module importing a web one is a direction
that only ever has to be untangled later.

## The absence that has to survive being drawn

`backup_runs` deliberately stores no "this destination is up to date" column,
because a flag like that only has to be wrong once. That decision is worth
nothing if a page quietly re-invents it — so there is no `current`, `synced` or
`up to date` anywhere in this module, and what is offered instead is the two
facts that *are* true:

    last attempted     whatever became of it
    last succeeded     a different question, and often a much older date

A destination attempted hourly and last successful in March is exactly the
state somebody needs to see, and one word cannot say it.

## A partial observation may not present itself as a whole one

`divergence.record` takes `complete` with no default because claiming a partial
listing was the whole destination is the one way to lose information. The same
rule applies to drawing it: a count from a comparison that did not finish is
shown with the date of the last one that did, never on its own. *412,338 only
at the destination — last complete comparison 3 September* is honest;
`412,338` alone would be a number pretending to be current.

## Disconnected is not a failure

A registered drive in a drawer produces no error, no warning and nothing red.
It produces a date. Half of what this module exists to do is make sure the
pages agree about that.

## A run that nobody finished

A process killed mid-transfer leaves a row saying `running`, for ever. Rather
than relabel it — inventing an outcome nobody observed — a run still `running`
long past the point where a transfer is abandoned is reported as **interrupted,
outcome unknown**, which is what is actually known about it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from librairy import backup_runs, destinations, divergence, offline_drives
from librairy.config import Settings
from librairy.destinations import (
    LOCAL,
    MODE_LABEL,
    MODE_MEANING,
    OFFLINE,
    REPORTING,
    Destination,
    Policy,
)
from librairy.humanize import human_ago, human_bytes
from librairy.transfer_paths import MARKER_ONLY
from librairy.transfer_run import redact

#  How many runs one destination shows before "see all". A page, not a log.
RECENT = 8


@dataclass(frozen=True)
class PolicyView:
    """`Photos → NAS Backup → Backup`, and whether it is switched on."""

    policy: Policy
    category: str
    category_label: str
    mode: str
    mode_label: str
    mode_meaning: str
    enabled: bool


@dataclass(frozen=True)
class RunView:
    """One run, in words, with nothing in it that could carry a credential."""

    id: int
    origin: str
    origin_label: str
    scope: str
    mode_label: str
    state: str
    outcome: str
    started: str
    started_ago: str
    finished: str
    transferred: int
    bytes_sent: int
    destination_only: int
    detail: str
    interrupted: bool = False

    @property
    def size(self) -> str:
        return human_bytes(self.bytes_sent)

    @property
    def result(self) -> str:
        """What became of it, including the answer nobody likes: we do not know.

        A row left `running` by a killed process is not quietly promoted to
        succeeded or demoted to failed — either would be an outcome nobody
        observed.
        """
        if self.interrupted:
            return "Interrupted — outcome unknown"
        return {
            backup_runs.SUCCEEDED: "Finished",
            backup_runs.FAILED: "Failed",
            backup_runs.RUNNING: "Running",
            backup_runs.PLANNED: "Starting",
        }.get(self.state, self.state)

    @property
    def summary(self) -> str:
        parts = []
        if self.transferred:
            parts.append(f"{self.transferred:,} copied")
        if self.bytes_sent:
            parts.append(self.size)
        if self.destination_only:
            parts.append(f"{self.destination_only:,} only at the destination")
        return " · ".join(parts) or "nothing to do"


@dataclass(frozen=True)
class DestinationView:
    """One place library content is copied to, and how well it is going."""

    destination: Destination
    name: str
    kind: str
    kind_label: str
    #  Never the raw target for a remote: an rclone remote is `name:path` and
    #  the name is safe, but a target somebody typed by hand is somebody's
    #  typing. Everything shown here has been through `redact`.
    target: str
    modes: tuple[str, ...]
    mode_labels: tuple[str, ...]
    enabled: bool
    offline: bool
    #  For an offline drive only. Empty everywhere else, because "connected" is
    #  not a question anybody asks about a NAS.
    presence: str = ""
    presence_note: str = ""
    verification: str = ""
    verification_note: str = ""
    reduced: bool = False
    wrong_drive: bool = False
    last_attempted: str = ""
    last_attempted_ago: str = ""
    last_succeeded: str = ""
    last_succeeded_ago: str = ""
    last_failed: bool = False
    interrupted: bool = False
    #  Only at the destination, and how well it is known.
    only_here: int = 0
    only_here_verified: str = ""
    only_here_complete: bool = True
    reports_divergence: bool = False
    policies: tuple[PolicyView, ...] = ()
    runs: tuple[RunView, ...] = field(default_factory=tuple)

    @property
    def reachable(self) -> bool:
        """As far as anybody knows. For an online destination this is unknown
        until something tries, so it reports what the last run found."""
        if self.offline:
            return self.presence == offline_drives.PRESENT
        return not self.last_failed

    @property
    def only_here_sentence(self) -> str:
        """The count, and never the count on its own after a partial scan.

        A number from a comparison that did not finish, shown alone, is a
        number pretending to be current.
        """
        if not self.only_here:
            return ""
        said = f"{self.only_here:,} only at the destination"
        if self.only_here_complete:
            return said
        when = self.only_here_verified or "never"
        return f"{said} — not fully verified, last complete comparison {when}"


@dataclass(frozen=True)
class Overview:
    """The Dashboard's whole share of this: counts, and what to link to.

    Deliberately small. A dashboard that grows a backup administration console
    stops being a dashboard, and everything below the first line of this has a
    page of its own that says it better.
    """

    destinations: int = 0
    healthy: int = 0
    failing: int = 0
    disconnected: int = 0
    wrong_drive: int = 0
    only_here: int = 0
    running: int = 0

    @property
    def any(self) -> bool:
        return self.destinations > 0

    @property
    def needs_looking_at(self) -> int:
        """A disconnected drive is not counted. It is where a drive lives."""
        return self.failing + self.wrong_drive


def destination_views(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    runs: bool = False,
    only_here: bool = True,
) -> list[DestinationView]:
    """Every configured destination, as the pages need it.

    Bounded by the number of destinations, which is a handful — one query per
    destination for its runs and one aggregate for its divergence. Presence is
    read from what the worker recorded and never probed here: a render must not
    stat a mount or start a subprocess.
    """
    del settings
    by_destination: dict[int, list[PolicyView]] = {}
    for policy in destinations.policies(conn):
        by_destination.setdefault(policy.destination_id, []).append(_policy(policy))
    found = []
    for destination in destinations.destinations(conn):
        found.append(
            _destination(
                conn,
                destination,
                tuple(by_destination.get(destination.id, ())),
                with_runs=runs,
                with_only_here=only_here,
            )
        )
    return found


def overview(conn: sqlite3.Connection, settings: Settings) -> Overview:
    """The Dashboard block. One pass over the same views, and one count.

    The count is asked once for every destination together rather than once
    each, because this runs on a five-second poll and the Dashboard does not
    break the number down. Measured at 1.3 million divergent rows: 139 ms as
    three per-destination counts, 65 ms as one — for a number that can only
    change when a comparison runs, which is hourly at most.
    """
    views = destination_views(conn, settings, only_here=False)
    if not views:
        return Overview()
    return Overview(
        destinations=len(views),
        healthy=sum(1 for view in views if view.enabled and view.reachable),
        failing=sum(1 for view in views if view.enabled and view.last_failed),
        disconnected=sum(
            1 for view in views if view.offline and view.presence == offline_drives.ABSENT
        ),
        wrong_drive=sum(1 for view in views if view.wrong_drive),
        only_here=divergence.count_all(conn),
        running=sum(1 for view in views if view.interrupted),
    )


def runs_for(
    conn: sqlite3.Connection, destination_id: int, limit: int = RECENT
) -> list[RunView]:
    return [_run(run) for run in backup_runs.recent(conn, destination_id, limit=limit)]


def _destination(
    conn: sqlite3.Connection,
    destination: Destination,
    policies: tuple[PolicyView, ...],
    *,
    with_runs: bool,
    with_only_here: bool,
) -> DestinationView:
    offline = OFFLINE in destination.modes and destination.kind == LOCAL
    here = offline_drives.presence(conn, destination.id) if offline else None
    attempted = backup_runs.last_run(conn, destination.id)
    succeeded = backup_runs.last_success(conn, destination.id)
    #  The last run that reached an outcome, which is not the last run. One
    #  still in flight must not erase what the previous one found.
    finished = backup_runs.last_finished(conn, destination.id)
    found = (
        divergence.summary(conn, destination.id)
        if with_only_here
        else divergence.Summary(destination_id=destination.id)
    )
    reports = any(policy.mode in REPORTING for policy in policies)
    return DestinationView(
        destination=destination,
        name=destination.name,
        kind=destination.kind,
        kind_label="This machine" if destination.kind == LOCAL else "rclone remote",
        target=redact(destination.target),
        modes=tuple(destination.modes),
        mode_labels=tuple(MODE_LABEL.get(mode, mode) for mode in destination.modes),
        enabled=destination.enabled,
        offline=offline,
        presence=here.state if here else "",
        presence_note=here.sentence if here else "",
        verification=here.verification if here else "",
        verification_note=(
            offline_drives.VERIFICATION_LABEL.get(here.verification, "") if here else ""
        ),
        reduced=bool(here and here.verification == MARKER_ONLY),
        wrong_drive=bool(here and here.refused),
        last_attempted=attempted.started_at if attempted else "",
        last_attempted_ago=human_ago(attempted.started_at) if attempted else "",
        last_succeeded=succeeded.started_at if succeeded else "",
        last_succeeded_ago=human_ago(succeeded.started_at) if succeeded else "",
        last_failed=bool(finished and finished.state == backup_runs.FAILED),
        interrupted=bool(attempted and attempted.unresolved),
        only_here=found.count,
        only_here_verified=(found.verified_at or "")[:10],
        only_here_complete=found.complete,
        reports_divergence=reports,
        policies=policies,
        runs=tuple(runs_for(conn, destination.id)) if with_runs else (),
    )


def _policy(policy: Policy) -> PolicyView:
    return PolicyView(
        policy=policy,
        category=policy.category,
        #  The folder the taxonomy actually files this category into, which is
        #  the name somebody recognises — "Music Videos", not "music_videos".
        category_label=_folder_label(policy.category),
        mode=policy.mode,
        mode_label=MODE_LABEL.get(policy.mode, policy.mode),
        mode_meaning=MODE_MEANING.get(policy.mode, ""),
        enabled=policy.enabled,
    )


def _folder_label(category: str) -> str:
    from librairy.transfer_plan import _folder  # noqa: PLC2701

    return _folder(category)


def _run(run: backup_runs.Run) -> RunView:
    from librairy.transfer_plan import MANUAL

    return RunView(
        id=run.id,
        origin=run.origin,
        origin_label=(
            "Sent from Browse" if run.origin == MANUAL else "Scheduled backup"
        ),
        scope=run.category,
        mode_label=MODE_LABEL.get(run.mode, run.mode),
        state=run.state,
        outcome=run.outcome,
        started=run.started_at,
        started_ago=human_ago(run.started_at),
        finished=run.finished_at,
        transferred=run.transferred,
        bytes_sent=run.bytes_sent,
        destination_only=run.destination_only,
        #  Redacted on the way in as well as here. Belt and braces, because
        #  this is the last place before a template.
        detail=redact(run.detail),
        interrupted=run.unresolved,
    )
