"""What is already at a destination — read, and only ever read.

One job, and the reason it is its own module is the shape of its return value:

    a list   this is what is there
    None     nobody could look

Those are different answers and collapsing them is the failure this whole
feature exists to prevent. An empty list means the destination is reachable and
holds nothing, so everything gets copied. `None` means a drive is in a drawer
or a remote did not answer, and the honest response is to say so rather than to
report a backup that is perfectly up to date with zero files.

## It cannot do anything but list

For a remote that is `rclone lsjson`, which reads. For a local destination it
is a directory walk, which reads. There is no branch here that writes, moves or
removes, and there is nothing to pass in that could make one — the only inputs
are a destination and a scope.

## Where a destination keeps things

Beneath the backup root, at the same path the file has in the Library:

    Library      Books/Programming/Rust/book.pdf
    destination  <root>/Books/Programming/Rust/book.pdf

One rule, no special trees, and it is what makes a one-off send of
`Books/Programming/Rust` and a scheduled backup of `Books` describe the same
files in the same places. Anything else — a `manual-exports/` folder, a
flattened category root — would make the two incomparable, and the second
comparison would propose copying everything again.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

from librairy import transfer_paths
from librairy.config import Settings
from librairy.destinations import LOCAL, OFFLINE, Destination
from librairy.tools import rclone
from librairy.transfer_paths import MARKER, TransferRefused
from librairy.transfer_plan import DestinationFile, Scope

LOGGER = logging.getLogger(__name__)

#  How long a listing may take. A remote that has not answered in two minutes
#  is a remote that is not answering, and a worker cycle should not wait on it.
TIMEOUT = 120


def listing(
    conn: sqlite3.Connection,
    settings: Settings,
    destination: Destination,
    scope: Scope,
) -> list[DestinationFile] | None:
    """Everything at the destination under this scope's folder, or None."""
    del conn
    folder = scope.directory
    try:
        if destination.kind == LOCAL:
            return _local(settings, destination, folder)
        return _remote(settings, destination, folder)
    except TransferRefused as refusal:
        LOGGER.info("cannot list %s: %s", destination.name, refusal)
        return None
    except (OSError, ValueError, json.JSONDecodeError):
        LOGGER.exception("listing %s failed", destination.name)
        return None


def _local(
    settings: Settings, destination: Destination, folder: str
) -> list[DestinationFile] | None:
    if destination.modes == (OFFLINE,) or destination.identity:
        target = transfer_paths.checked_offline(
            settings, destination.target, destination.identity, destination.volume
        ).path
    else:
        target = transfer_paths.local_destination(settings, destination.target).path
        if not target.is_dir():
            return None
    #  Exactly `<root>/<folder>`, with no fallback to the root itself. An
    #  earlier version fell back, which read the first category's files as
    #  every category's — fine while a drive held one, and silently wrong the
    #  moment it held two. A folder that is not there yet is an empty listing,
    #  which is true: nothing has been copied there.
    root = target / folder if folder else target
    if not root.is_dir():
        return []
    found: list[DestinationFile] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == MARKER:
            continue
        here = path.relative_to(root).as_posix()
        found.append(
            DestinationFile(
                relpath=f"{folder}/{here}" if folder else here,
                size=path.stat().st_size,
            )
        )
    return found


def _remote(
    settings: Settings, destination: Destination, folder: str
) -> list[DestinationFile] | None:
    target = transfer_paths.remote_destination(destination.target)
    config = settings.appdata_dir / "rclone" / "rclone.conf"
    if not rclone.rclone_status(config).available:
        return None
    completed = rclone.run(
        rclone.lsjson_command(config, f"{target.rstrip('/')}/{folder}"),
        timeout=TIMEOUT,
    )
    if completed.returncode != 0:
        #  Not an empty destination. A remote that said no is a remote nobody
        #  could look at, and reporting that as "nothing there" would propose
        #  copying the entire library to somewhere that is not answering.
        return None
    return [
        DestinationFile(relpath=f"{folder}/{entry['Path']}", size=int(entry.get("Size", 0)))
        for entry in json.loads(completed.stdout or "[]")
        if not entry.get("IsDir")
    ]


def sample(root: Path, limit: int = 5) -> list[str]:
    """A few names from a directory, for a settings page to prove it can see it."""
    if not root.is_dir():
        return []
    return [path.name for path in sorted(root.iterdir())[:limit]]
