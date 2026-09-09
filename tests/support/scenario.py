"""One installation, one user story, and the rules that hold across all of them.

Each feature has its own tests and each of them passes. That is not the same as
the features holding together, and the difference is where the expensive defects
live: a tag that survives four transitions and is lost on the fifth, a backup
that reads a path the Library stopped using an hour ago, a recovery that fixes
the journal and leaves the index. None of those is visible from inside the
feature that causes them, because inside that feature nothing is wrong.

So the scenarios in `tests/test_cross_feature_scenarios.py` do not test
features. They perform a coherent piece of ordinary use — analyse, decide,
commit, tag, back up, undo, crash, carry on — and then ask the questions no
single feature can answer.

`assert_sound` is those questions. It is asked at every interesting moment of
every scenario, so a scenario about backups will fail if backups quietly break
the index, and none of them has to remember to check.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from librairy import commit_state, tags
from librairy.classify import analyze_items
from librairy.config import Settings
from librairy.db import connect
from librairy.executor import ExecutionSummary, execute_plan
from librairy.fingerprint import blake2b_file
from librairy.history import undo_plan
from librairy.paths import is_in_flight
from librairy.planner import approve_plan, create_plan
from librairy.scanner import scan_root, visible_files

ROOTS = ("inbox", "library", "quarantine")

#  One commit or one reversal, killed at a named instant. Written to disk and
#  run as its own process because the point is a process that stops existing.
CHILD = '''
import errno, os, shutil, signal, sys
from librairy import executor, history
from librairy.config import Settings
from librairy.db import connect

where, plan_id = sys.argv[1], sys.argv[2]


def die(*_args, **_kwargs):
    os.kill(os.getpid(), signal.SIGKILL)


if where == "after_bytes":
    #  The file is in the library; not one row says so yet.
    executor._finish_op = die
elif where == "after_journal":
    #  The journal knows. The index does not.
    executor._move_item_row = die
elif where == "mid_copy":
    #  A cross-filesystem move — inbox and library on different mounts, which
    #  is the ordinary NAS arrangement — killed while the bytes are copying.
    def no_rename(src, dst):
        raise OSError(errno.EXDEV, "forced cross-device")

    def half_copy(src, dst):
        data = open(src, "rb").read()
        with open(dst, "wb") as handle:
            handle.write(data[: len(data) // 2])
        die()

    os.rename = no_rename
    shutil.copy2 = half_copy
elif where == "undo_after_bytes":
    #  The file is back in the inbox; the reversal is not journalled.
    history._record_undo = die
elif where == "undo_after_journal":
    #  The reversal is journalled; the index still names the library.
    history._update_item_after_undo = die
else:
    raise SystemExit(f"unknown crash point: {where}")

settings = Settings(_env_file=None)
conn = connect(settings)
if where.startswith("undo"):
    history.undo_plan(conn, plan_id, settings)
else:
    executor.execute_plan(conn, plan_id, settings)
'''



@dataclass
class Installation:
    """A LibrAIry somebody uses, with the ordinary verbs on it.

    Deliberately thin. Every method here goes through the same function the web
    route or the CLI goes through — the point of a scenario is that the real
    path is taken, and a harness that reimplements approval would be testing the
    harness.
    """

    settings: Settings
    conn: sqlite3.Connection
    tmp_path: Path
    _client: object = field(default=None, repr=False)

    #  --- the verbs ---------------------------------------------------------

    def root(self, name: str) -> Path:
        return {
            "inbox": self.settings.inbox_dir,
            "library": self.settings.library_dir,
            "quarantine": self.settings.quarantine_dir,
        }[name]

    def write(self, root: str, relpath: str, content: bytes) -> Path:
        path = self.root(root) / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def scan(self, *roots: str) -> None:
        for name in roots or ROOTS:
            scan_root(self.conn, name, self.root(name), self.settings)

    def analyze(self):  # noqa: ANN201 - librairy.classify.AnalyzeSummary
        return analyze_items(self.conn, self.settings)

    def client(self):  # noqa: ANN201 - fastapi.testclient.TestClient
        """The web application, signed in, as a person would meet it."""
        if self._client is None:
            from fastapi.testclient import TestClient

            from librairy.web.app import create_app

            self._client = TestClient(create_app(self.settings, self.conn))
            self._client.post("/setup", data={"password": "correct horse battery"})
        return self._client

    def post(self, url: str, **data: object):  # noqa: ANN201 - httpx.Response
        client = self.client()
        return client.post(
            url, data=data, headers={"x-csrf-token": client.cookies["csrf_token"]}
        )

    def plan_for(self, specs: list) -> str:
        plan_id = create_plan(self.conn, specs, self.settings)
        approve_plan(self.conn, plan_id, self.settings)
        return plan_id

    def commit(self, plan_id: str) -> ExecutionSummary:
        return execute_plan(self.conn, plan_id, self.settings)

    def undo(self, plan_id: str) -> list:
        return undo_plan(self.conn, plan_id, self.settings)

    def crash(self, plan_id: str, where: str) -> None:
        """Run one commit or reversal in a real process, and kill it mid-operation.

        The seam is patched in the child, so nothing in the shipped code knows a
        drill is running. `SIGKILL` rather than an exception: an exception
        unwinds, and unwinding is what a power cut does not do.
        """
        script = self.tmp_path / "crash_child.py"
        script.write_text(CHILD, encoding="utf-8")
        env = os.environ.copy()
        env.update(
            {
                "APPDATA_DIR": str(self.settings.appdata_dir),
                "INBOX_DIR": str(self.settings.inbox_dir),
                "LIBRARY_DIR": str(self.settings.library_dir),
                "QUARANTINE_DIR": str(self.settings.quarantine_dir),
                "FILE_STABILITY_SECONDS": "0",
            }
        )
        done = subprocess.run(  # noqa: S603 - our own interpreter, our own script
            [sys.executable, str(script), where, plan_id],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        assert done.returncode == -9, (
            f"the {where} drill did not reach its crash point: {done.stderr[-800:]}"
        )

    #  --- what is true right now --------------------------------------------

    def bytes_at(self, root: str) -> dict[str, str]:
        """Every visible file under a root, by fingerprint. A comparable state."""
        base = self.root(root)
        return {
            relpath: blake2b_file(base / relpath)
            for relpath in visible_files(base, self.settings.ignore_patterns)
        }

    def live(self, root: str = "") -> list[sqlite3.Row]:
        where = " AND root=?" if root else ""
        return list(
            self.conn.execute(
                f"SELECT * FROM items WHERE missing_since IS NULL{where} ORDER BY id",  # noqa: S608
                (root,) if root else (),
            )
        )

    def tags_of(self, item_id: int) -> set[str]:
        return {str(tag["tag"]) for tag in tags.for_item(self.conn, item_id)}

    def item_at(self, root: str, relpath: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM items WHERE root=? AND relpath=?", (root, relpath)
        ).fetchone()

    def item(self, item_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()


def install(tmp_path: Path, **overrides: object) -> Installation:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        AUTH_REQUIRED=False,
        #  Nothing here reaches a provider. The scenarios that are *about*
        #  provider behaviour say so, and stub the provider rather than the
        #  program around it.
        OLLAMA_HOST="",
        _env_file=None,
        **overrides,
    )
    for name in ROOTS:
        Path(
            {
                "inbox": settings.inbox_dir,
                "library": settings.library_dir,
                "quarantine": settings.quarantine_dir,
            }[name]
        ).mkdir(parents=True, exist_ok=True)
    settings.appdata_dir.mkdir(parents=True, exist_ok=True)
    return Installation(settings=settings, conn=connect(settings), tmp_path=tmp_path)


#  --- the rules that hold everywhere ------------------------------------------


def assert_sound(inst: Installation, *, unfinished: int = 0, phantoms: int = 0) -> None:
    """What has to be true of an installation, whatever has just happened to it.

    Asked between the steps of every scenario. Each one of these was a real
    defect somewhere, or the direct consequence of one, and none of them is a
    claim about a feature — they are claims about the installation as a whole.
    """
    _every_live_row_is_a_file(inst)
    _no_half_written_file_is_indexed(inst)
    _no_run_is_called_running_without_the_lock(inst, unfinished)
    _no_tag_points_at_nothing(inst)
    _nothing_missing_is_really_here_under_another_name(inst, phantoms)


def _every_live_row_is_a_file(inst: Installation) -> None:
    """A live row promises a file at a path. Two rows cannot promise one file.

    `items` has UNIQUE (root, relpath), so distinct live rows are distinct
    addresses — which makes "no duplicate live item for one physical file"
    exactly this check: every live row's file is really there. A row that is not
    is either a move that was recorded and not made, or an index that was left
    behind by one that was.
    """
    for row in inst.live():
        path = inst.root(str(row["root"])) / str(row["relpath"])
        assert path.is_file(), (
            f"the index claims {row['root']}/{row['relpath']} is there, and it is not"
        )


def _no_half_written_file_is_indexed(inst: Installation) -> None:
    """LibrAIry's own incomplete transfer artefact is never user data.

    Not merely hidden from a page: it must be impossible for the indexer to give
    a `.part-<plan>` file an `items` row, because a row is what makes it
    browsable, searchable, countable and eligible for backup. See
    `paths.IN_FLIGHT` and `docs/architecture/crash-recovery.md`.
    """
    for row in inst.conn.execute("SELECT root, relpath FROM items"):
        name = str(row["relpath"]).rpartition("/")[2]
        assert not is_in_flight(name), (
            f"{row['root']}/{row['relpath']} is a half-written file with an index row"
        )


def _no_run_is_called_running_without_the_lock(inst: Installation, expected: int) -> None:
    """`executing` is a row; the lock is what knows whether anything is running."""
    stopped = commit_state.unfinished(inst.conn, inst.settings)
    assert len(stopped) == expected, (
        f"{len(stopped)} interrupted commit(s), expected {expected}: "
        f"{[one.plan_id for one in stopped]}"
    )


def _no_tag_points_at_nothing(inst: Installation) -> None:
    """A tag belongs to an item, not to a path, so a move cannot strand one.

    Project membership is these rows — `tags` promoted to a Project is still the
    same rows — so a stale tag row is a Project that lists a file the library no
    longer has.
    """
    stranded = list(
        inst.conn.execute(
            "SELECT t.tag, t.item_id FROM item_tags t"
            " LEFT JOIN items i ON i.id = t.item_id"
            " WHERE i.id IS NULL"
        )
    )
    assert not stranded, f"tags on items that do not exist: {stranded}"


def _nothing_missing_is_really_here_under_another_name(
    inst: Installation, allowed: int = 0
) -> None:
    """No phantom: a row reported gone whose bytes are live under another row.

    This is what a crash used to leave behind — the inbox row still naming a
    file LibrAIry had already filed, and a second row for the library copy — and
    it is exactly what somebody sees as *a file vanished* about a file that is
    sitting in their library. Reconcile exists for the case where a **person**
    moved something; nothing in these scenarios does, so any phantom here is
    LibrAIry disagreeing with itself.
    """
    phantoms = list(
        inst.conn.execute(
            "SELECT gone.root, gone.relpath, found.root AS at_root,"
            "       found.relpath AS at_relpath"
            "  FROM items gone JOIN items found"
            "    ON found.fingerprint = gone.fingerprint AND found.id <> gone.id"
            "   AND found.missing_since IS NULL"
            " WHERE gone.missing_since IS NOT NULL"
            "   AND gone.fingerprint IS NOT NULL AND gone.fingerprint <> ''"
        )
    )
    assert len(phantoms) == allowed, (
        f"{len(phantoms)} row(s) reported missing whose bytes are live elsewhere, "
        f"expected {allowed}: {[tuple(row) for row in phantoms]}"
    )
