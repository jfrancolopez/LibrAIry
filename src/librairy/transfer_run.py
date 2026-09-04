"""Doing what the plan said, and refusing to work anything else out.

The planner decides *what*. This decides *nothing* — it takes a plan, checks
that the world still looks the way it did when the plan was made, and asks
rclone to move the files the plan already named.

## No generic "run an rclone operation"

There is no function here that takes a command, or a verb, or a list of
options. The only way to make bytes move is to hand `send` a plan, and a plan
can only contain `copy` and `update` because those are the only actions
`destinations.ACTIONS` can produce. Deletion is unrepresentable two layers up
and there is no door into it here either — the same pattern the policy
vocabulary uses, carried through to execution.

`tools/rclone.py` keeps its own gate underneath, twice over: an allowlist of
verbs, an allowlist of options, and a refusal of destructive ones. Belt, braces
and a second pair of braces, because the thing being guarded against is a
future edit that looks reasonable.

## A plan is not trusted forever

Between planning and copying, the world can move: a source can be replaced by a
symlink, a drive can be pulled, the mount point it leaves behind is a directory
that will happily accept files. So everything is checked again **immediately
before** launching, not once at planning time:

    the source still resolves inside the Library
    the destination is still somewhere else entirely
    the offline drive is still attached, still carries its marker, and is
      still the same filesystem

The window between that check and the copy is as small as it can be made. It is
not zero, and pretending otherwise would be worse than saying so.

## Interruption

rclone is left to be recoverable rather than made to be: a killed process, a
dropped network or a full disk leaves whatever it finished, and the next run
compares again and copies what is still missing. Convergence comes from the
comparison being cheap and repeatable, not from a resume protocol.

**A failed run is never recorded as current.** That is the one bookkeeping rule
here, and it is the difference between a backup that is behind and a backup
that is behind and says it is fine.

## Nothing here touches the Library

Not its files, not its rows, not a proposal, not a taxonomy. A backup
succeeding or failing changes what is known about a *destination* and nothing
about what a file is. See `tests/test_transfer_run.py`.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from librairy import divergence, transfer_paths
from librairy.config import Settings
from librairy.destinations import LOCAL, MIRROR, OFFLINE
from librairy.planner import utc_now
from librairy.tools import rclone
from librairy.transfer_paths import TransferRefused
from librairy.transfer_plan import Plan, transfers

LOGGER = logging.getLogger(__name__)

#  How long one transfer may run before it is abandoned. Generous, because a
#  first backup of a photo library is genuinely hours — and finite, because a
#  wedged mount should not hold a worker for ever.
TIMEOUT = 6 * 60 * 60

#  Why a run stopped, in categories a person can act on differently. Not free
#  text: "it failed" is the least useful thing a backup can tell somebody.
OK = "ok"
REFUSED = "refused"  # a check said no; nothing was attempted
UNAVAILABLE = "unavailable"  # the destination was not there
MISSING_TOOL = "missing-tool"  # rclone is not installed
FULL = "full"  # the destination ran out of room
INTERRUPTED = "interrupted"  # killed, timed out, connection dropped
FAILED = "failed"  # rclone said no and none of the above fit

#  What rclone says it moved. Parsed from its own summary rather than counted
#  by us, because after a failure our count is a guess and this is evidence:
#  "Transferred: 73 / 100, 73%" is rclone reporting what it actually did.
_TRANSFERRED = re.compile(r"Transferred:\s+(\d+)\s*/\s*(\d+)")

#  What a secret looks like in a command or in output. Redaction happens on the
#  way *into* the record, never on the way out to a screen, so nothing that was
#  never stored can be leaked by a later template.
_SECRETS = re.compile(
    r"(?i)(--?(?:pass|password|user-pass|token|key|secret|client-secret|auth)"
    r"[= ]\s*)(\S+)|([a-z0-9+/=]{24,}:[^@\s]+@)|(https?://[^/\s:]+:[^@\s]+@)"
)


@dataclass(frozen=True)
class Result:
    """What one run did. Structured, because parsed log text is not an API."""

    outcome: str
    files: int = 0
    bytes_sent: int = 0
    seconds: float = 0.0
    started_at: str = ""
    finished_at: str = ""
    detail: str = ""
    #  The command, with anything that looks like a credential removed. Kept
    #  for diagnostics and safe to show, because it was redacted before it was
    #  stored rather than before it was displayed.
    command: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.outcome == OK

    @property
    def current(self) -> bool:
        """May this run be recorded as leaving the destination up to date?

        Only a clean one. A run that copied nine files out of ten did useful
        work and did not finish, and a backup that is behind while saying it is
        fine is worse than one that is behind and says so.
        """
        return self.outcome == OK


def send(
    conn,  # noqa: ANN001 - sqlite3.Connection; unused today, held for run history
    settings: Settings,
    plan: Plan,
    *,
    runner: Callable[[list[str], int], subprocess.CompletedProcess[str]] | None = None,
) -> Result:
    """Copy what the plan named, and nothing it did not.

    `runner` exists so the safety tests can watch the exact argv without a
    filesystem or a network. It is not a way to run something else: whatever it
    is handed has already been through `tools/rclone.py`.
    """
    del conn
    started = utc_now()
    began = time.monotonic()
    if plan.unavailable:
        return Result(
            outcome=UNAVAILABLE,
            started_at=started,
            finished_at=utc_now(),
            detail=plan.unavailable,
        )
    work = transfers(plan)
    if not work:
        return Result(outcome=OK, started_at=started, finished_at=utc_now())

    try:
        source, target = _checked(settings, plan)
    except TransferRefused as refusal:
        #  A check said no. Nothing was attempted, and that is a different
        #  outcome from a transfer that started and broke.
        return Result(
            outcome=REFUSED,
            started_at=started,
            finished_at=utc_now(),
            detail=str(refusal),
        )

    status = rclone.rclone_status(_config(settings))
    if not status.available and plan.destination.kind != LOCAL:
        return Result(
            outcome=MISSING_TOOL,
            started_at=started,
            finished_at=utc_now(),
            detail=status.detail,
        )

    #  Ask rclone to say what it moved. Without this a run that failed halfway
    #  has no evidence of how far it got, and "73 files did reach the
    #  destination" would have to be either invented or thrown away.
    command = rclone.copy_command(
        _config(settings), source, target, stats=True
    )
    safe = redacted(command)
    try:
        completed = (runner or _run)(command, TIMEOUT)
    except subprocess.TimeoutExpired:
        return _result(INTERRUPTED, started, began, "the transfer timed out", safe)
    except OSError as exc:
        return _result(MISSING_TOOL, started, began, str(exc), safe)

    moved = _moved(completed.stderr or "")
    if completed.returncode != 0:
        #  `moved` and not nought. A run that copied seventy-three of a hundred
        #  files did seventy-three files' worth of good, and that is true
        #  whatever happened next — it is simply not permission to call the
        #  destination current, which nothing in this program stores anyway.
        #  See `librairy/backup_runs.py`.
        return _result(
            _why(completed.stderr or ""),
            started,
            began,
            redact(completed.stderr or "")[:400],
            safe,
            files=moved,
        )
    return _result(
        OK,
        started,
        began,
        "",
        safe,
        #  rclone's own count where it gave one, and the plan's otherwise: a
        #  clean run transferred what differed, which is what the plan named.
        files=moved or len(work),
        bytes_sent=plan.bytes_to_send,
    )


def run_policy(
    conn,  # noqa: ANN001 - sqlite3.Connection
    settings: Settings,
    policy,  # noqa: ANN001 - destinations.Policy
    destination,  # noqa: ANN001 - destinations.Destination
    listing: list | None,
    *,
    runner: Callable[[list[str], int], subprocess.CompletedProcess[str]] | None = None,
) -> tuple[Plan, Result]:
    """Compare, record, transfer, record. The whole of one backup.

    The order matters and it is the point: the run row is opened **before**
    anything moves, so a process killed mid-transfer leaves a row saying it was
    running rather than leaving nothing at all. An absence cannot be told from
    a run that never started, and "we do not know how that ended" is a thing
    this has to be able to say.
    """
    from librairy import backup_runs
    from librairy.transfer_plan import plan_for

    plan = plan_for(conn, policy, destination, listing)
    if plan.unavailable:
        #  Nobody could look. Not a run, because nothing was attempted — and
        #  recording it as a failed one would fill the history of a drive that
        #  lives in a drawer with failures that are just Tuesdays.
        return plan, Result(
            outcome=UNAVAILABLE,
            started_at=utc_now(),
            finished_at=utc_now(),
            detail=plan.unavailable,
        )
    if policy.mode == MIRROR:
        #  The whole of what Mirror adds, and the only place the two modes
        #  differ: what is only at the destination is written down so it can be
        #  read. Recorded before the transfer rather than after, because it is
        #  a fact about the comparison and stays true whether or not the copy
        #  that follows it succeeds. See `librairy/divergence.py`.
        divergence.record(
            conn,
            destination_id=destination.id,
            category=policy.category,
            entries=plan.reported,
            count=plan.destination_only,
        )
    run_id = backup_runs.begin(
        conn,
        destination_id=destination.id,
        category=policy.category,
        mode=policy.mode,
        planned_copies=plan.to_copy,
        planned_updates=plan.to_update,
        destination_only=plan.destination_only,
    )
    result = send(conn, settings, plan, runner=runner)
    backup_runs.finish(
        conn,
        run_id,
        succeeded=result.ok,
        transferred=result.files,
        bytes_sent=result.bytes_sent,
        outcome=result.outcome,
        detail=result.detail,
    )
    return plan, result


def _checked(settings: Settings, plan: Plan) -> tuple[Path, str]:
    """Everything re-verified, as late as it can be.

    A plan made ten minutes ago describes a world that may have moved. The
    offline case is the sharp one: a drive pulled between planning and copying
    leaves a mount point behind, and a mount point is a directory.
    """
    source = transfer_paths.library_source(settings, _folder_of(plan))
    destination = plan.destination
    if destination.kind == LOCAL:
        if plan.policy.mode == OFFLINE:
            checked = transfer_paths.checked_offline(
                settings, destination.target, destination.identity, destination.volume
            )
        else:
            checked = transfer_paths.local_destination(settings, destination.target)
            if not checked.path.is_dir():
                raise TransferRefused(f"{destination.name} is not there")
        return source, str(checked.path)
    return source, transfer_paths.remote_destination(destination.target)


def _folder_of(plan: Plan) -> str:
    from librairy.transfer_plan import _folder  # noqa: PLC2701, PLC0415

    return _folder(plan.policy.category)


def redact(text: str) -> str:
    """Remove anything that looks like a credential. Applied before storing."""
    return _SECRETS.sub(lambda match: _mask(match), str(text))


def redacted(command: list[str]) -> tuple[str, ...]:
    """The command as it may be shown. Redacted here, not at the template.

    A secret that never reaches the record cannot be leaked by a page that
    forgets to hide it, by a log line, or by a History entry somebody exports.
    """
    return tuple(redact(part) for part in command)


def _mask(match: re.Match[str]) -> str:
    if match.group(1):
        return f"{match.group(1)}***"
    return "***@"


def _moved(stderr: str) -> int:
    """How many files rclone says it transferred, or 0 if it did not say.

    Evidence rather than arithmetic. Zero here means "rclone did not tell us",
    which is reported as nothing having been proven to move rather than as
    nothing having moved — the two are different and only one of them is a
    claim this program is entitled to make.
    """
    found = _TRANSFERRED.search(stderr)
    return int(found.group(1)) if found else 0


def _why(stderr: str) -> str:
    """Which kind of failure this was, from what rclone said.

    Categories rather than a sentence, because a full destination and a dropped
    connection want different things from a person and "backup failed" wants
    nothing from them but worry.
    """
    text = stderr.lower()
    if "no space left" in text or "quota" in text or "insufficient" in text:
        return FULL
    if any(
        phrase in text
        for phrase in ("connection", "timeout", "timed out", "broken pipe", "reset by peer")
    ):
        return INTERRUPTED
    if "directory not found" in text or "no such host" in text or "not found" in text:
        return UNAVAILABLE
    return FAILED


def _result(
    outcome: str,
    started: str,
    began: float,
    detail: str,
    command: tuple[str, ...],
    *,
    files: int = 0,
    bytes_sent: int = 0,
) -> Result:
    return Result(
        outcome=outcome,
        files=files,
        bytes_sent=bytes_sent,
        seconds=round(time.monotonic() - began, 3),
        started_at=started,
        finished_at=utc_now(),
        detail=detail,
        command=command,
    )


def _config(settings: Settings) -> Path:
    return settings.appdata_dir / "rclone" / "rclone.conf"


def _run(command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    """The only place a transfer actually starts.

    `rclone.run` re-checks the command it is given, so this cannot become a way
    to execute something the builders would have refused.
    """
    return rclone.run(command, timeout=timeout)
